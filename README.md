# 青汀区抄表日账仓

Python 3.11 标准库和 SQLite。站员提交具名、有出处的读数批，仓管按指定批序冻结基础日账，异地夜班凭固定来件独立入账及核对。源仓和隔离账各用一个数据库文件。交接使用由源仓 `export` 领取的正式来件文本。

## 运行

在项目根目录执行。

```sh
python3 -B check_project.py
python3 -B -m audit.handover demo
python3 -B -m audit.handover init --source source.sqlite --ledger ledger.sqlite
```

控制台物化项目时提供 `check_project.py`，它运行 `tests` 包的完整原回归。演示在临时目录中初始化源仓和两份隔离账，显式导入 `samples/day.json`。它验证基础读数、两种到件顺序和重开后的交班报告，并在断言成功后输出实际业务结果。初始化只补齐表及索引，已有业务资料和用户自建表保留。

## Python 接口

源连接上构造 `intake.store.BatchStore`、`freezing.store.FrozenDayStore`、`issue.register.IssueRegister`。隔离连接上构造 `receive.store.AcceptanceStore`。各存储对象均有 `initialize()`，连接由调用方持有及关闭。写入口自行管理事务，在连接无外层事务时调用。同一仓的并行调用使用独立连接。

- `intake.batches.submit(batches, batch_id, station, day, rows)` 返回原 `StationBatch` 和 `ImportReceipt`。日期使用 `YYYY-MM-DD`。行按输入顺序排列，形如 `{'unit': 'L', 'litres': 7, 'source': {'document': 'station.csv', 'line': 2}}`。`litres` 接收整数或 `None`。来源行号为正的 SQLite 整数，批内位置从 1 开始另行保存。`get_batch` 和 `get_receipt` 可按号复领。
- `freezing.days.freeze(batches, days, day, batch_ids)` 返回 `FrozenDay`，日账号为其 `day_id`。至少选一批，同一批只选一次。成员按给定批序，明细按批内顺序。`freezing.history.load_frozen_day(days, day_id)` 可重领源仓快照。
- `issue.corrections.amend(days, register, correction_id, day_id, station, delta)` 签发归属成员站的整数升差额并返回更正号。`issue.withdrawals.withdraw(register, withdrawal_id, correction_id)` 签发针对确切已发行更正的撤销并返回撤销号。同一更正只允许一个撤销号。正式操作号不得以 `day:` 开头，该前缀供基础日账号使用。
- `issue.register.export(days, register, package_id)` 返回版本 1 的 JSON 来件文本。基础件首次领取时正式发行。各来件在发行时固定，后续重领返回原文本。仓间可独立传递文本。
- `receive.intake.accept(ledger, wire, before_commit=None)` 返回 `AcceptanceReceipt`。可选回调在初次实收写入后、提交前接收待提交回执，回调抛出的异常使该次实收整体回滚。成功重投直接复领原回执。等价的 JSON 空白及对象字段排列不改变规范内容。
- `audit.daybook.read(ledger, day_id=...)` 返回固定基础快照、实收小计和实际分录。以 `package_id=...` 调用可领原实收回执。两个查询参数择一，未知号返回 `None`。`audit.handover.prepare_handover(ledger, day_id, package_ids)` 在同一读快照内核对日账及指定来件的回执。

无效输入、同号异内容、非法归属或冲突关联抛出 `ValueError`。发行找不到目标日账或更正、冻结找不到指定批、实收缺少目标基础时抛出 `LookupError`。SQLite 错误及提交检查点异常向调用方传递，原已提交数据可继续领取。

## 计量与核对

`None` 表示缺测，原行和出处仍保留。实测零和缺测可分别核对。分站基础量为所选批的实测整数升之和，无实测的站保留 0 L。基础、差额和累计量按整数精确处理，持久表示使用十进制文本。分站实收量是该站基础量加上该站实际分录，全日量由各站量加总。

隔离账裁定只用已提交的本地数据。样例的基础量为 20 L，C 为 +5 L，D 为 +4 L，R 撤销 C。先实收 R 时提交无分录结论，C 迟到也无分录，整日只留下 D，余额依次为 20、24、24 L。实收 C、D、R 时保留原更正并追加一次反记，余额依次为 25、29、24 L，实际分录为 +5、+4、−5 L。两条路径都能复领每件回执。分录按本地提交序列出，分录中保留触发来件号和原更正号。
