# 观澜第十六轮：无损调度、正常模型路线与预测身份

工程实现与本轮有界验证已完成：固定b7源码本地 **1567项Python、11组浏览器通过**；direct正常入口的A初始、A卖出后、B现金三阶段均取得有限报告。持续运行不能标全通过：environment为170次HTTP/165次完整EOF，direct为168/165，所有失败保持；内容最低交付与完整事实核验分开，部分报告存在明确事实或措辞缺陷。

断流调查复现了当前所选代理路径约50秒正文空闲关闭，最新对应样本由本机7890先发FIN；direct跨55秒静默控制通过，但另有3次不同形态ReadError尚不能唯一定位。日常数据库、WAL/SHM、.env与232冻结文件均未改变；验收封存时尚未提交或推送，日常服务未重启。正式自然生成和自然成熟效果仍未覆盖。

本轮从 `3da5f191c464e27120bd0f739ef2355e10122070` 的干净工作区开始，验收期间未回退、提交或推送。第十五轮三份有限业务报告与全部历史失败保持原状；不将分阶段的历史成功计作本轮固定版本成功率。日常实例与数据的只读起点见 [保全基线](sixteenth/preservation-before.json)。本地 8765 当时无监听，日常库 schema 11。

2026年11月5日验收封存后，用户明确授权将本轮改动提交并推送至 `main`。保全与验收 JSON 保留各自记录时点的状态，不因后续推送回写；实际提交及远端 CI 状态以 Git 与 GitHub Actions 为准。

## 实现

首次正式批次先保存批次、样本、日期与原截止。同账户已有冻结或在途研究时显示 `waiting_research`，不改变 `latest_job_id`；旧报告仍可合法发表。未领取且目标范围兼容的任务允许明确合并，已经发给模型的输入不追认新范围。worker 在任务边界先处理等待正式批次，再派发普通持久需求。暂停研究阻止启动，不阻止原截止到期。真实买卖与范围变化仍使旧版本失效；取消原因保存为结构化版本更新、同版本替换或历史未知。详见 [调度策略与生产链回归](sixteenth/scheduling.md)。

运行设置新增模型专用 `model_network_route`：默认 `environment` 保留 HTTPX 环境代理/NO_PROXY 语义，显式 `direct` 仅忽略模型连接的环境代理。TLS 与主机名校验保持，环境 CA 优先级保持。所有角色、公共研究、目录与能力检测共用策略；任务冻结路线及修订，目录和能力指纹随配置变化。没有自动路线切换或未知请求重发。配置标签、CONNECT 实际观察与不可见物理链路分别记录；行情/PDF 客户端和系统网络不变。详见 [Mac 操作手册](mac-operations.md)。

报告系统标识来自持久化批次与预测身份，明确 formal/exploratory/test/demo、基准日、发布时间、信息截止和目标交易日，不根据正文词语升级类型。原文导出 `original` 保持 UTF-8 字节；阅读导出 `reading` 加系统元数据，两者有不同文件名和各自 SHA-256。历史正文、预测和应用 `content_assessment=not_evaluated` 不改写。预测上下文在 seed 顶层保留，避免挤占运行状态中的子任务配额。既有 `return_period` 与 `moving_average_period` 继续使用，20个收盘观察/19个间隔与21个观察/20个间隔通过实际出站上下文检查。

本轮不增加数据库表或改写历史，schema 仍为11；新增兼容接口，API contract仍为13。所有写入验收使用独立数据库和随机端口。源码更新不代表日常实例已升级，日常启停继续遵循手册中的备份与目标确认流程。

独立审阅补齐了两处上下文边界：100目标页不再被身份字段重复复制；汇总重建时过大目标页显式归档，保留批次身份、原截止、预期数量与分页位置，未读目标仍为未覆盖。小预算时完全相同的模型配置可引用同一请求中的配置，原计算值、期间、归属和持久检查点不变。没有扩大上下文限额。另将 HTTP 401/403 归入等待配置，保留原 HTTP 错误诊断；同一拒绝配置不随定时事件反复请求，配置修订后经现有指纹规则恢复。

## 验收方式与边界

首轮开发期全套 Python 结果为 **1539 passed、10 failed、1 warning**，见 `test-results/sixteenth/python-initial.txt`。失败包括新路线修订字段导致完整设置回写被拒、增量预测上下文挤占小上下文子任务列表，以及调度合并后旧测试仍假定创建新任务/单一原因字符串。修复保留原上下文预算、事务回滚和幂等断言；最终固定源码结果另列，不用开发期测试重标为最终完整回归。

第一份冻结源码 `9e6ec0b8…` 的完整 Python 为 **1559 passed、1 warning / 205.56s**（`python-frozen-v1.txt`）。随后独立审阅发现上述多目标小预算与认证跨调度边界，故这次通过仍保留原源码身份，不冒充后来修复后的最终全套。浏览器首轮未显式提供项目 Python，多个临时服务未启动，已中断并保存 `browser-frozen-v1-first.txt`；使用 `PYTHON="$PWD/.venv/bin/python"` 后 production 浏览器组通过，没有修改任何测试时间保护。

完整需求与受控回归定位见 [回归映射](sixteenth/regression-map.md)，各次失败与补测说明见 [模型路线](sixteenth/model-route.md) 和 [语义/多事件](sixteenth/semantics-and-events.md)。这些定向组与完整套件有重叠，不相加为唯一用例数。

最终冻结源码为 `b7afd9860b14304d6c1f698e7fbaed5db80435d37c6f90db920000324c806470`，232个源文件/测试/锁文件哈希见 [冻结清单](sixteenth/frozen-source.json)。同一源码完整 Python 为 **1567 passed、1 warning / 207.24s**，十一组浏览器全部退出0；已逐文件核对未漂移。警告来自 Starlette TestClient 的 HTTPX 弃用提示，未为消除提示更换依赖。见 [最终工程结果](sixteenth/engineering-final.json)。本机 Python 3.14.4，HTTPX 0.28.1；没有声称另在 Linux 或新远端提交重跑。

真实验证入口为 `scripts/verify_sixteenth_live.py --run-real`：实际 `manage.py` 子进程，使用正常 HTTP API 建立模拟账本，接受 A/B/公共范围变化产生的合法任务。无 transport 注入、单任务 guard 或生产时钟覆盖。每任务保留来源调用上限（本次40），声明已启动任务数观察阈值12及4小时边界；每2秒检查且在已启动数大于12时正常停止，并非发起前硬上限，可能包含同轮并发超过阈值的任务；到边界正常停止独立服务并保留后续任务，不把合法额外事件改成 failed。隔离 max_job_attempts=1 限制进程中断恢复次数，与每任务40次HTTP上限分别记录。对未知模型结果不自动重试业务阶段，明确拒绝仅按原生产策略有限重试。费用未获得供应商账单关联时记未知，不写零。

真实启动首次被自动审批拒绝，未执行、模型 HTTP 为0。随后核对仅复制模型连接/目录/角色配置，A/B及全部成交为新建模拟数据，不复制日常账户、账本、报告或材料；明确目的地为已配置 `https://api.xxyapi.com/v1`，重新提交同一命令获准启动。用户随后再次明确确认该场景已获授权。见 [授权、目的地与数据范围证据](sixteenth/real-validation-authorization-scope.json)。该审批事件与供应商传输失败分开记录。

11月4日22:39（Asia/Shanghai）核对为交易日收盘后，已过默认正式窗口。日历依据为[上交所2026年休市安排](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)。受控正式发布与历史日期评分属于工程验证；本次真实账户默认未启用正式批次，不把探索报告、受控时钟或历史行情算成真实前瞻样本。自然1/5/20日效果需实际到期后另行记录，不为本轮改窗口、回填或空等。

正常长期入口仍是 `start.command`；在预测设置中提前启用账户规则，保持服务在原生成窗口运行，才能积累正式样本。暂停模型研究时可保留预测评估开关，到期评分继续按原目标日期读取数据。未来自然结果不属于本轮后台代跑承诺。

最终源码、各验收层结果、真实报告身份与保全复查在本页后续记录中分别列出。验收封存时尚未推送，因此该时点没有本轮新提交的远端 CI；后续推送不将本地结果改署为远端结果。

## environment正常矩阵的实际结果

同一冻结b7源码于11月4日23:16:16至11月5日00:15:50运行，9项已启动任务及所有合法后续事件均走正常worker。总计 **170次模型HTTP、165次completed+EOF、5次未获得完整终止对象**；源码未漂移。正常停止、重新启动后报告和预测均保持。它取得实际业务交付，但最小三阶段没有全通过，不能写成连续稳定成功。详见[最终执行记录](sixteenth/environment-execution-final.json)、[逐任务矩阵](sixteenth/live-matrix.json)和[全部模型请求索引](sixteenth/live-request-index.json)。

| 实际事件 | 模型HTTP / 完整EOF | 持久交付及独立内容复核 |
|---|---:|---|
| A初始三持仓v4 | 38 / 37 | 报告7f3c7432…；有限内容接受，一项子任务断流与原文读取限制保留 |
| A一次模拟卖出v5 | 10 / 9 | 无新报告，经理断流；内容未评估 |
| 候选范围更新A v6 | 29 / 28 | 无新报告，经理汇总断流；不能回署为卖出成功 |
| 候选范围更新B v2 | 37 / 37 | 报告f8e9ab0c…；有限内容接受，材料最终可见范围部分核实 |
| 公共candidate_scan | 11 / 11 | 公共有限报告；发布时需求快照与后续材料路由状态分别记录，未全覆盖 |
| B合法公共材料事件 | 11 / 11 | 报告a49f6a53…；有限内容接受，价格/均线比值“百分点”措辞错误保留 |
| A合法公共材料事件 | 11 / 10 | 无新报告，经理汇总断流；内容未评估 |
| 暂停定时研究所触发A配置事件 | 12 / 11 | 首次终止提交缺completion被契约拒绝，预算内纠正时断流，无正文 |
| 同次设置保存所触发B配置事件 | 11 / 11 | 报告67d7c75d…；有限内容接受，指数调权上限的时点/对象解释保留限定 |

报告原文、应用`content_assessment=not_evaluated`和全部旧预测未改。A卖出经正常接口及幂等重放核对：黄金200→100、现金+995、已实现−7.50，未重复成交或费用；原报告按版本合法stale。开发期内容复核分别见[A初始](sixteenth/a-initial-content-review.md)、[A卖出](sixteenth/a-after-sell-content-review.md)、[A范围更新](sixteenth/a-universe-update-content-review.md)、[A公共材料](sixteenth/a-public-materials-content-review.md)、[A配置事件](sixteenth/a-configuration-content-review.md)及[B三份报告索引](sixteenth/environment-b-content-review-index.md)。[公共任务与来源隔离](sixteenth/live-route-public-review.md)没有把模型观点当作账户间公共事实；[实际HTTP导出](sixteenth/live-export-verification.json)验证原文与阅读版的各自字节和哈希。

本矩阵18项均为探索预测：4 down、1 up、13 abstain；正式0、到期评估0。模型服务端工具用量和原始usage按任务/HTTP保留，缺少用量的失败请求不推为零；没有供应商账单关联，货币费用仍未知，未擅自换算ticks。

## 追加断流调查与显式direct验证

用户提供的供应商侧23:48:05、00:02:00、00:15:35，与三个本地失败精确同秒。五次异常距离最后HTTP正文字节均约50秒，各自300秒语义空闲/900秒硬限未到，后继任务和停机都发生在失败之后。第五次额外抓到7890代理端先发FIN，项目2.575毫秒后回发FIN；不能把`upstream_transport_error`字段名当作模型供应商责任。

受控本机HTTP/TLS55秒静默均可完成，既有Mihomo入口加本地DIRECT也通过。随后两条无模型、无认证、无业务资料的公共长静默响应对照复现：经过与供应商相同选中代理链时，首字节后50.040秒断开；direct跨过两段约55秒后完整结束。这支持当前所选代理路径的空闲关闭边界，尚未唯一定位该链中的具体部件。证据与方案见[断流调查](sixteenth/stream-disconnect-investigation.md)、[代理空闲对照](sixteenth/public-idle-route-control.md)、[供应商时间关联](sixteenth/supplier-time-correlation.json)。这两条GET不混入模型请求或费用分母。

为验证方案，另用同一b7源码、新独立库和正常manage/API入口显式选direct，执行一次有界完整业务矩阵；未重放旧未知请求、未修改日常/系统代理、TLS或任务限时。它使用新的任务身份，结果另记[direct矩阵](sixteenth/direct-matrix.json)和[逐请求索引](sixteenth/direct-request-index.json)，不抹去原environment失败，也不是同payload的模型随机对照。该矩阵已于11月5日01:11:08正常结束并完成实际重启，最终结果如下。

机器可读的最终分层结论见[验收汇总](sixteenth/acceptance-final.json)。所有工程开发中失败、不同冻结版本与真实尝试索引见[验证与失败索引](sixteenth/attempt-index.md)；正常启动、隔离验证和正式自然积累入口见[复现说明](sixteenth/reproduce.md)。

## direct最终结果与验收分层

direct正常矩阵为11月5日00:17:20至01:11:08。9项任务实际启动，6 completed、3 failed；168次模型HTTP中165次完整EOF。所有请求均direct/revision0、生产HTTPX、TLS验证开启、无新CONNECT，输出上限字段均省略。实际套接字也观察到服务进程直接连接供应商HTTPS地址；这不声称看见全部物理网络层。模型只使用grok-4.6/high、grok-4.7/medium或xhigh。

| 事件 | HTTP / 完整EOF | 报告或边界 |
|---|---:|---|
| A初始v4 | 39 / 39 | 9520a02f…，3679字符；有限账户内容接受 |
| A模拟卖出v5 | 11 / 11 | bdc30541…，3628字符；账户变化核对通过，营收同比摘要错误使完整事实核验不通过 |
| B初始v2 | 11 / 11 | e9cecf33…，2734字符；有限内容接受，收盘低点/日内低点与流动性表述有边界 |
| A候选更新v6 | 29 / 29 | 3d717c5d…，4367字符；有限组合及候选内容接受，不追认v5事实错误为已纠正 |
| 公共candidate_scan | 39 / 39 | 有限公共报告；305项发布需求快照未覆盖，后续304项完成路由确认，1项仍未覆盖，302项相关性待定 |
| B公共材料 | 11 / 11 | ca5bb5d2…，2568字符；有限内容接受，需求仅2/4完成 |
| A公共材料 | 10 / 9 | ReadError；无新报告/预测，未评估 |
| A配置事件 | 11 / 10 | ReadError；无新报告/预测，未评估 |
| B配置事件 | 7 / 6 | ReadError；无新报告/预测，未评估 |

三次direct ReadError分别距最后正文约185.93、142.84、99.94毫秒，均未到应用时限；没有据错误名归责模型可用性。现有底层异常链与关闭意图记录不足，不能恢复实际发起端，也不能把前一条代理先FIN抓包倒签给这些请求。见[direct读取错误调查](sixteenth/direct-read-error-investigation.md)。这就是最终执行标记仍为`matrix_incomplete_content_review_pending`的原因：原脚本分开持久发布与离线内容复核，并保留任何合法任务失败；本轮离线复核虽已完成，没有回写私有执行原件来擦除历史标记。

原文身份、完整哈希与开发期分项判断见[A初始](sixteenth/direct-a-initial-content-review.md)、[A卖出](sixteenth/direct-a-after-sell-content-review.md)、[A后续](sixteenth/direct-a-followup-content-review.md)、[B索引](sixteenth/direct-b-content-review-index.md)、[公共与来源隔离](sixteenth/direct-public-review.md)。[正常HTTP导出](sixteenth/direct-export-verification.json)核验了两份A与初始B报告。原文未改，应用依然not_evaluated；“接受有限账户回答”不代表所有财务事实、来源可见范围或交易执行条件已经通过。36项预测均为探索弃权，正式0、实际到期评估0。

| 验收层 | 最终结论 |
|---|---|
| 工程回归 | 通过：1567项Python、11组浏览器；同一冻结b7 |
| 正常启动与路线 | 通过受控保存/切换/重启/证书回归；两轮真实正常进程分别观察environment与direct |
| 真实模型传输 | 部分失败：environment 170/165；direct 168/165。两条非模型GET不混入此分母 |
| 终止契约与持久发布 | 已发布报告均经生产契约/事务；未知半截不发表、不自动重发。direct最低三业务阶段均有实际报告 |
| 实质内容 | 有来源的有限内容复核已完成；明确事实、单位和可见范围问题保留，不把生产not_evaluated升为已核验 |
| 正式预测生产链 | 受控完整生成、等待、截止、评分/修订/重启通过；夹具不算真实前瞻 |
| 正式自然生成 | 未执行：两轮独立业务账户未启用正式，且实际启动均不在生成窗口 |
| 自然到期效果 | 未观察；无真实正式样本/准确率，探索与弃权不补分母 |
| 持续事件与重启 | 受控协作通过；两轮真实合法任务均实际处理至终态，仍有失败/未覆盖/相关性待定；重启保持报告和预测，不宣称长期无人值守零故障 |

两轮合计338次模型HTTP/330次完整EOF仅用于完整尝试清单，不是同分布成功率实验；已知货币费用仍null，原始token/服务端工具用量与ticks按各请求留存。没有为维持近期100%继续重跑。

[最终保全](sixteenth/preservation-final.json)确认日常.env、DB、WAL、SHM逐字节哈希不变，232冻结文件无漂移，8765与两次验收端口62369/50825均无监听。两个独立服务已正常停止；未修改系统代理、证书或DNS。日常服务仍需按既有手册由用户明确部署，源码与本地验收不等于日常进程已升级；验收封存时远端CI尚未更新。
