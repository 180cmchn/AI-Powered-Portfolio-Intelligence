"""Local-only secret references. New secrets live in macOS Keychain, never SQLite.

Each credential revision is a separate Keychain item. Rotation keeps running
snapshots usable; explicit revoke removes all revisions belonging to a provider.
Legacy environment references are read-only and fingerprint checked on resume.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import uuid


class CredentialError(ValueError):
    pass


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class KeychainCredentials:
    service = 'guanlan.portfolio.providers'

    def _libraries(self):
        if sys.platform != 'darwin':
            raise CredentialError('安全凭据存储不可用：新供应商 Key 需要 macOS 钥匙串；未保存明文替代')
        cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        ptr, lng = ctypes.c_void_p, ctypes.c_long
        cf.CFStringCreateWithCString.argtypes = [ptr, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ptr
        cf.CFDataCreate.argtypes = [ptr, ptr, lng]
        cf.CFDataCreate.restype = ptr
        cf.CFDictionaryCreate.argtypes = [ptr, ctypes.POINTER(ptr), ctypes.POINTER(ptr), lng, ptr, ptr]
        cf.CFDictionaryCreate.restype = ptr
        cf.CFDataGetLength.argtypes = [ptr]
        cf.CFDataGetLength.restype = lng
        cf.CFDataGetBytePtr.argtypes = [ptr]
        cf.CFDataGetBytePtr.restype = ptr
        cf.CFRelease.argtypes = [ptr]
        sec.SecItemAdd.argtypes = [ptr, ctypes.POINTER(ptr)]
        sec.SecItemAdd.restype = ctypes.c_int32
        sec.SecItemCopyMatching.argtypes = [ptr, ctypes.POINTER(ptr)]
        sec.SecItemCopyMatching.restype = ctypes.c_int32
        sec.SecItemDelete.argtypes = [ptr]
        sec.SecItemDelete.restype = ctypes.c_int32
        return cf, sec

    def _operate(self, account: str, action: str, secret: str | None = None):
        cf, sec = self._libraries()
        ptr = ctypes.c_void_p
        owned = []
        def const(name, library=sec):
            return ptr.in_dll(library, name).value
        def string(value):
            item = cf.CFStringCreateWithCString(None, value.encode(), 0x08000100)
            owned.append(item)
            return item
        entries = [(const('kSecClass'), const('kSecClassGenericPassword')),
                   (const('kSecAttrService'), string(self.service)),
                   (const('kSecAttrAccount'), string(account))]
        if action == 'put':
            data = secret.encode()
            buf = ctypes.create_string_buffer(data)
            item = cf.CFDataCreate(None, ctypes.cast(buf, ptr), len(data))
            owned.append(item)
            entries.append((const('kSecValueData'), item))
        elif action == 'get':
            entries.extend([(const('kSecReturnData'), const('kCFBooleanTrue', cf)),
                            (const('kSecMatchLimit'), const('kSecMatchLimitOne'))])
        keys = (ptr * len(entries))(*(k for k, _ in entries))
        vals = (ptr * len(entries))(*(v for _, v in entries))
        query = cf.CFDictionaryCreate(None, keys, vals, len(entries),
                   ctypes.byref(ctypes.c_byte.in_dll(cf, 'kCFTypeDictionaryKeyCallBacks')),
                   ctypes.byref(ctypes.c_byte.in_dll(cf, 'kCFTypeDictionaryValueCallBacks')))
        owned.append(query)
        result = ptr()
        try:
            status = (sec.SecItemAdd(query, None) if action == 'put' else
                      sec.SecItemDelete(query) if action == 'delete' else
                      sec.SecItemCopyMatching(query, ctypes.byref(result)))
            if action == 'delete' and status == -25300:
                return None
            if status:
                raise CredentialError(f'系统钥匙串操作失败（状态 {status}）；请解锁钥匙串后重试，未退化为明文存储')
            if action == 'get':
                return ctypes.string_at(cf.CFDataGetBytePtr(result), cf.CFDataGetLength(result)).decode()
            return None
        finally:
            if result.value:
                cf.CFRelease(result)
            for item in reversed(owned):
                cf.CFRelease(item)

    def put(self, secret: str) -> dict:
        ref = 'guanlan-' + uuid.uuid4().hex
        self._operate(ref, 'put', secret)
        return {'kind': 'keychain', 'id': ref, 'fingerprint': fingerprint(secret)}

    def get(self, reference: dict) -> str:
        return resolve_credential(reference, self)

    def delete(self, reference: dict):
        if reference.get('kind') == 'keychain':
            self._operate(reference['id'], 'delete')


def resolve_credential(reference: dict | None, vault=None) -> str:
    if not reference or reference.get('kind') == 'missing':
        raise CredentialError('供应商尚未配置 API Key')
    if reference.get('kind') == 'environment':
        value = os.environ.get(reference['name'], '')
    elif reference.get('kind') == 'keychain':
        value = (vault or KeychainCredentials())._operate(reference['id'], 'get')
    else:
        raise CredentialError('无法识别的安全凭据引用')
    if not value:
        raise CredentialError('供应商凭据不存在或已撤销；请重新输入 API Key')
    if reference.get('fingerprint') != fingerprint(value):
        raise CredentialError('冻结的供应商凭据已变更；本轮不会静默改用新 Key，请创建新任务')
    return value
