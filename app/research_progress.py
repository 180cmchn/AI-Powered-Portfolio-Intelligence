"""Progress comparison for narrowly identified read-only business lookups.

Only the comparison copy changes. Actual tool outputs, timestamps, payloads and
archives remain intact. All other tool rounds retain the strict old signature.
"""
import copy
import json
from collections import OrderedDict

from .calculation_context import signature


_BUSINESS_LOOKUPS = frozenset({'list_business_demands', 'list_forecast_targets',
                             'list_recommendations', 'list_agent_roles'})


_NATIVE_MATERIAL_METADATA_FIELDS = frozenset({
    'id', 'document_version_id', 'document_version', 'version', 'legacy_version', 'previous_id',
    'change_kind', 'change_summary', 'changed_at',
    'fetched_at', 'retrieved_at', 'delivered_at', 'updated_at',
    'observation_status', 'observation_sequence', 'observation_started_at',
    'observation_result_status', 'is_latest_document_observation',
})


def _native_material_content(value):
    if isinstance(value, dict):
        return {key: _native_material_content(item) for key, item in value.items()
                if key not in _NATIVE_MATERIAL_METADATA_FIELDS}
    if isinstance(value, list):
        return [_native_material_content(item) for item in value]
    return value


def native_material_identity(item):
    """Compare source evidence even when a returned excerpt gets a new storage version."""
    return json.dumps(_native_material_content(item), sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), default=str)


def tool_round_signature(results):
    values = [(call.name, call.arguments, output) for call, output in results]
    if values and all(name in _BUSINESS_LOOKUPS for name, _, _ in values):
        normalized = []
        for name, arguments, output in values:
            output = copy.deepcopy(output)
            if isinstance(output, dict):
                if name == 'list_forecast_targets':
                    # forecast_job_context sets this receipt time on every read;
                    # target dates, deadlines and baseline/preparation times stay.
                    output.pop('delivered_at', None)
                elif name == 'list_recommendations':
                    # review_recommendations rewrites this refresh stamp even
                    # when its business state is unchanged. Keep all state,
                    # quote timestamps, prices, revisions and nested content.
                    for item in output.get('items', []) if isinstance(output.get('items'), list) else []:
                        if isinstance(item, dict):
                            item.pop('updated_at', None)
            normalized.append((name, arguments, output))
        # These reads do not depend on one another. Compare the same multiset
        # even when call order changes; preserve duplicates and all row order.
        values = sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True,
                        ensure_ascii=False, default=str))
    return signature('tool_round', {}, values)


_KNOWN_LOOKUPS = _BUSINESS_LOOKUPS | frozenset({'list_materials', 'search_materials', 'get_recommendation'})
_LOOKUP_REFRESH_FIELDS = {'list_forecast_targets': 'delivered_at', 'get_recommendation': 'updated_at'}


def _known_lookup_output(name, output):
    if not isinstance(output, dict):
        return output
    if name in _LOOKUP_REFRESH_FIELDS:
        # Detail and list expose the same persisted recommendation refresh stamp.
        return {key: value for key, value in output.items() if key != _LOOKUP_REFRESH_FIELDS[name]}
    if name == 'list_recommendations' and isinstance(output.get('items'), list):
        return {**output, 'items': [
            {key: value for key, value in item.items() if key != 'updated_at'} if isinstance(item, dict) else item
            for item in output['items']]}
    return output


def is_failed_tool_output(output):
    if not isinstance(output, dict):
        return False
    if output.get('failed') or output.get('error') or output.get('ok') is False:
        return True
    status = output.get('status')
    return isinstance(status, str) and status.strip().lower() in {'failed', 'error'}


class KnownLookupTracker:
    """Last complete result per exact query in one bounded research loop."""

    def __init__(self, max_entries=128):
        self.max_entries = max_entries
        self._entries = OrderedDict()

    def observe(self, results):
        all_tracked = bool(results) and all(call.name in _KNOWN_LOOKUPS for call, _ in results)
        entries = []
        for call, output in results:
            if call.name not in _KNOWN_LOOKUPS:
                continue
            if not all_tracked and is_failed_tool_output(output):
                continue
            key = (call.name, json.dumps(call.arguments, sort_keys=True, separators=(',', ':')))
            fingerprint = signature('known_lookup', {}, _known_lookup_output(call.name, output))
            entries.append((key, fingerprint))

        # Compare every call against the pre-round cache, including duplicates.
        all_known = all_tracked and all(self._entries.get(key) == fingerprint for key, fingerprint in entries)
        for key, fingerprint in entries:
            self._entries[key] = fingerprint
            self._entries.move_to_end(key)
            if len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return all_known
