# 离线考试试卷包校验与发放 CLI (exam-dispatch)

一个面向离线考试场景的试卷包校验、批次发放、状态追溯、安全回滚命令行工具。

## 特性

- **导入预演 (preview)**：拿到考场 CSV 和配置 JSON 后，先汇总即将创建的批次、考场源文件、目标文件名、版本、人数校验和潜在冲突，不写出任何试卷包；预演结果按批次持久化，重启 CLI 后仍可 query 查到，export JSON/CSV 时自动附带预演摘要
- **结构化预检 (dry-run)**：科目/版本/人数/源文件路径/目标文件名多重校验
- **目标冲突检测**：复制前发现重复目标名立即报错终止
- **批次发放**：支持目录复制或 zip 打包两种模式
- **状态持久化**：批次状态、配置快照、预检/预演/发放/回滚报告落盘，重启不丢失
- **安全回滚**：校验 SHA256，发现目标文件被替换时停止并给出证据
- **交接审计包**：一键打包配置快照、各类报告、事件日志为离线 zip，内置 manifest + SHA256 防篡改校验
- **数据导出**：支持 JSON、批次 CSV、发放明细 CSV（导出结果自动包含预演摘要）

## 安装

```bash
pip install -r requirements.txt
# 或
pip install -e .
```

安装后获得 `exam-dispatch` 命令（也可以直接 `python -m exam_paper_dispatcher.cli`）。

## 目录结构

```
.
├── examples/
│   ├── config.json              # 配置样例
│   ├── rooms.csv                # 正常考场清单
│   ├── rooms_conflict.csv       # 故意冲突的清单（用于演示）
│   └── papers/                  # 样例试卷源文件
│       ├── paper_math_v1.pdf
│       ├── paper_chinese_v1.pdf
│       └── paper_english_v2.pdf
├── exam_paper_dispatcher/       # 源码
└── .exam_dispatch_state/        # 默认持久化目录（运行后自动创建）
```

## 配置文件 (JSON)

```json
{
  "source_root": "examples/papers",
  "output_root": "examples/output",
  "default_subjects": ["math", "chinese", "english"],
  "subject_versions": {
    "math": "2026-spring-v1",
    "chinese": "2026-spring-v1",
    "english": "2026-spring-v2"
  },
  "package_format": "zip",
  "naming_pattern": "{exam_id}_{room_id}_{subject}_{version}",
  "storage_dir": ".exam_dispatch_state"
}
```

| 字段 | 说明 |
|---|---|
| source_root | 试卷源文件根目录 |
| output_root | 发放输出目录 |
| default_subjects | 允许的科目列表 |
| subject_versions | 科目-版本映射 |
| package_format | `dir` 或 `zip` |
| storage_dir | 持久化状态目录 |

## 考场清单 (CSV)

```csv
exam_id,room_id,subject,students_count,source_file,target_name
20260608,A101,math,30,paper_math_v1.pdf,20260608_A101_math
20260608,A102,math,28,paper_math_v1.pdf,20260608_A102_math
```

必填列：`exam_id`, `room_id`, `subject`, `students_count`, `source_file`, `target_name`。

## 命令速览

```
exam-dispatch preview       --config ... --rooms ...     # 第0步：导入预演（可选，不写出试卷包）
exam-dispatch precheck      --config ... --rooms ...     # 第1步：预检
exam-dispatch dispatch      --batch-id ...               # 第2步：发放
exam-dispatch query         [--batch-id ...]             # 第3步：查询
exam-dispatch rollback      --batch-id ... [--force]     # 第4步：回滚
exam-dispatch audit-pack    --batch-id ... --output ...  # 打包：生成交接审计包
exam-dispatch audit-verify  --archive ...                # 验包：校验审计包完整性
exam-dispatch export        --format ... --output ...    # 导出数据
```

---

## 第 0 步：导入预演 (preview / 可选)

正式预检前，可先跑一遍 `preview`，把即将创建的批次、每个考场会使用的源文件、目标文件名、版本、人数校验结果和潜在冲突全部汇总，但**不会写出任何试卷包**，方便先交给考务复核。

```bash
python -m exam_paper_dispatcher.cli preview \
  --config examples/config.json \
  --rooms examples/rooms.csv
```

- 输出：
  - 汇总面板（通过/问题、各错误项计数）
  - 预演明细表（考场、科目、版本、人数、源文件、源路径、目标文件名、目标路径）
  - 潜在冲突表（目标路径重复、目标磁盘已存在同名文件）
  - 缺失源文件 / 目标名冲突 / 非法科目 / 版本问题 表格
  - 警告列表
- 退出码：始终为 `0`（预演本身不阻断，问题通过表格与警告呈现）

关键特性：

- **不写出任何试卷包**：`output_root` 不会被创建或写入。
- **同一 batch-id 重复预演不覆盖旧记录**：每次 `preview` 都会生成新的 `preview-*` ID，按时间顺序追加保存。
- **持久化**：预演结果随批次落盘，重启 CLI 后 `query` 与 `export` 均能看到预演摘要。
- **路径按规则解析**：`storage_dir`、`source_root`、`output_root` 按相对于配置文件所在目录解析并以绝对路径展示，便于复核。

示例：先预演成功但磁盘已有同名输出 -> 预演会在潜在冲突中列出，考务据此决定是否清理、改名或继续预检/发放。

---

## 四步标准流程演示

### 第 1 步：预检 (precheck / dry-run)

```bash
python -m exam_paper_dispatcher.cli precheck \
  --config examples/config.json \
  --rooms examples/rooms.csv
```

- 输出：
  - 汇总面板（通过/失败、各错误项计数）
  - 缺失源文件表格（行号、考场、科目、解析路径）
  - 目标文件名冲突表格
  - 非法科目 / 人数问题 / 版本问题表格
- **退出码**：
  - `0` 预检通过
  - `2` CSV 格式/列缺失
  - `3` 存在缺失源文件
  - `4` 存在目标名冲突
  - `5` 非法科目或人数
  - `6` 版本未配置

> 关键行为：即使 `--persist` 开启，**预检失败时批次状态只会标记为 `failed`，绝不会误标为已完成**。使用 `--no-persist` 时完全不落盘。

验证冲突场景：

```bash
python -m exam_paper_dispatcher.cli precheck \
  --config examples/config.json \
  --rooms examples/rooms_conflict.csv
# 退出码 = 4
```

---

### 第 2 步：发放 (dispatch)

预检通过后，使用输出的 `batch-*` ID 执行发放：

```bash
python -m exam_paper_dispatcher.cli dispatch --batch-id <上一步的批次ID>
```

- 根据 `package_format` 生成目录副本或 zip 包
- 每个目标文件计算 SHA256 并记录
- **退出码**：
  - `0` 全部成功
  - `7` 批次不存在
  - `8` 批次已完成（需先回滚）
  - `10` I/O 错误

---

### 第 3 步：查询 (query)

```bash
# 列出所有批次
python -m exam_paper_dispatcher.cli query

# 按状态过滤
python -m exam_paper_dispatcher.cli query --status completed

# 查询单批次详情 (JSON)
python -m exam_paper_dispatcher.cli query --batch-id <批次ID>
```

列表显示：批次 ID、状态、创建/更新时间、项目数、成功/失败、备注。
详情 JSON 包含：配置快照、预检报告、预演摘要、发放明细（含 SHA256）、回滚记录。

---

### 第 4 步：回滚 (rollback)

```bash
# 安全回滚（校验 SHA256，文件被替换会停止）
python -m exam_paper_dispatcher.cli rollback --batch-id <批次ID>

# 强制回滚（跳过校验）
python -m exam_paper_dispatcher.cli rollback --batch-id <批次ID> --force
```

- **退出码**：
  - `0` 回滚完成
  - `7` 批次/报告不存在
  - `9` 检测到目标文件被替换或删除失败，回滚被中断

> 当某目标文件的实际 SHA256 与发放时记录的不一致时，回滚立即在该条目处停止，并打印：
> - 目标文件路径
> - 期望 SHA256
> - 实际 SHA256
>
> 防止误删他人放置的同名文件。

---

## 交接审计包 (audit-pack / audit-verify)

考务人员在班组交接或离线归档时，可把某个批次（预检、发放、回滚任意阶段）完整打包成一个可离线校验的 zip，包含：配置快照、预检报告、发放明细、回滚记录、相关事件日志、一份人工可读 README 和一份 manifest。**重启 CLI 后从持久化状态重新生成，内容完全一致（除生成时间戳外）。**

### 1. 打包 (audit-pack)

```bash
# 生成审计包（输出文件已存在时默认拒绝）
python -m exam_paper_dispatcher.cli audit-pack \
  --batch-id <批次ID> \
  --output out/audit_batch-001.zip

# 强制覆盖已存在的输出
python -m exam_paper_dispatcher.cli audit-pack \
  --batch-id <批次ID> \
  --output out/audit_batch-001.zip \
  --force
```

归档包含以下文件（按需出现，未到该阶段则不包含对应报告）：

| 文件 | 说明 |
|---|---|
| `config_snapshot.json` | 配置 + CSV 路径 + 保存时间快照 |
| `precheck_report.json` | 预检报告（含明细项） |
| `dispatch_report.json` | 发放明细（含每个目标 SHA256），若已发放才会出现 |
| `rollback_report.json` | 回滚记录（逐条动作和原因），若已回滚才会出现 |
| `batch_events.log` | 该批次相关的事件日志（过滤自全局 events.log） |
| `README.txt` | 人工可读摘要：批次号、状态、统计、文件清单、使用说明 |
| `manifest.json` | 校验清单：schema 版本、批次号/状态、配置摘要 SHA256、各类明细数量、所有文件 SHA256 |

**生成前检查**（失败时退出码、错误信息可被脚本识别，且不会留下半截 zip）：

| 检查项 | 失败退出码 | 常见原因 |
|---|---|---|
| 批次存在 | `7` | batch-id 拼写错误 |
| 批次状态非 pending | `23` | 批次刚创建但尚未执行预检 |
| 必需报告（配置快照、预检报告）存在 | `22` | 存储目录被手工清理或 `--no-persist` |
| 输出目录可写 | `21` | 目录权限不足或磁盘只读 |
| 同名输出冲突 | `20` | 指定路径已有文件，未加 `--force` |

- **退出码 `0`** 表示打包成功，zip 文件原子落盘。
- 其他失败场景（磁盘写满、编码错误等）返回通用 `10`（IO_ERROR）。

### 2. 验包 (audit-verify)

```bash
python -m exam_paper_dispatcher.cli audit-verify \
  --archive out/audit_batch-001.zip
```

校验流程按以下顺序执行，任一失败都会把错误逐条打印到 stderr 并以退出码 `24` 结束：

1. **文件存在且为合法 zip** —— 不是 zip 或已损坏直接失败
2. **manifest.json 存在且可解析** —— 缺少 manifest 视为无效归档
3. **每个文件 SHA256 与 manifest 匹配** —— 检测归档被第三方篡改或传输损坏
4. **批次号一致** —— `precheck_report.json` 中的 `batch_id` 与 manifest 声明一致
5. **明细数量一致** —— manifest 中的 `counts.precheck_items / dispatch_items / rollback_results` 与报告文件内实际条目数相等
6. **配置摘要一致** —— `config_snapshot.json` 中 `config` 字段的规范排序哈希与 `manifest.config_digest_sha256` 一致

校验通过时打印摘要表格（批次 ID、状态、明细数量、文件数等）并返回退出码 `0`；
校验失败时 stderr 形如：

```
审计包校验失败: out/audit_broken.zip
  - 文件 SHA256 不匹配: precheck_report.json (期望 24daeb3e4265..., 实际 1a58af24e5c3...)
  - manifest 声明的文件在归档中缺失: config_snapshot.json
```

### 3. 失败排查速查表

| 现象 / 退出码 | 可能原因 | 排查 / 解决 |
|---|---|---|
| `20` AUDIT_OUTPUT_CONFLICT | 输出路径已有 zip | 换一个文件名，或追加 `--force` 覆盖 |
| `21` AUDIT_OUTPUT_PERMISSION | 输出目录不可写 | 检查目录权限、磁盘是否满、是否只读挂载 |
| `22` AUDIT_MISSING_REPORT | 配置快照或预检报告缺失 | 确认 batch-id 正确；确认预检时使用了默认 `--persist` |
| `23` AUDIT_INVALID_BATCH_STATUS | 批次处于 pending | 先跑一次 `precheck` 再打包 |
| `24` AUDIT_VERIFY_FAILED | 归档被篡改或损坏 | 重新打包；怀疑传输问题时用 `audit-verify` 对照源端 |
| `7` BATCH_NOT_FOUND | 批次不存在 | 检查 `--storage-dir` 是否指向正确；核对 batch-id 拼写 |

---

## 数据导出

重新启动 CLI 后仍可导出所有历史数据：

```bash
# 全部数据 JSON（批次 + 事件日志，含预演摘要）
python -m exam_paper_dispatcher.cli export --format json --output out/all.json

# 批次摘要 CSV（含预演次数字段）
python -m exam_paper_dispatcher.cli export --format csv --output out/batches.csv

# 单个批次的发放明细 CSV
python -m exam_paper_dispatcher.cli export --format csv-items \
  --batch-id <批次ID> --output out/items.csv
```

---

## 完整退出码表

| 退出码 | 常量 | 含义 |
|---|---|---|
| 0 | SUCCESS | 成功 |
| 1 | CONFIG_ERROR | 配置文件错误 |
| 2 | INVALID_CSV | CSV 清单格式错误或缺失 |
| 3 | MISSING_SOURCE | 缺失试卷源文件 |
| 4 | TARGET_CONFLICT | 目标文件名冲突 |
| 5 | INVALID_SUBJECT | 非法科目或人数 |
| 6 | INVALID_VERSION | 科目版本未配置 |
| 7 | BATCH_NOT_FOUND | 批次不存在或状态不符 |
| 8 | BATCH_ALREADY_DONE | 批次已发放，需先回滚 |
| 9 | ROLLBACK_TAMPERED | 回滚时发现文件被替换，已停止 |
| 10 | IO_ERROR | 文件复制/删除/打包等 I/O 错误 |
| 11 | BATCH_ID_CONFLICT | 自定义批次 ID 已存在，拒绝复用覆盖 |
| 20 | AUDIT_OUTPUT_CONFLICT | audit-pack 输出文件已存在（未加 --force） |
| 21 | AUDIT_OUTPUT_PERMISSION | audit-pack 输出目录不可写或无法创建 |
| 22 | AUDIT_MISSING_REPORT | audit-pack 缺少配置快照或预检报告 |
| 23 | AUDIT_INVALID_BATCH_STATUS | audit-pack 目标批次仍为 pending，尚无报告可打包 |
| 24 | AUDIT_VERIFY_FAILED | audit-verify 发现归档篡改、缺失或内容不一致 |
| 99 | UNKNOWN_ERROR | 未预期的异常 |

---

## 异常链路说明

1. **缺失试卷文件 -> dry-run 不落已完成批次**
   - `precheck` 时如果发现 `missing_sources`，`report.passed = false`
   - 即使 `--persist`，批次状态只会写入 `failed`，绝不会出现 `completed`
   - 使用 `--no-persist` 时完全不写存储

2. **两个清单行指向同一目标名 -> 复制前报冲突**
   - `precheck` 阶段对 `target_name` 做 group by
   - 任何重复都会进入 `target_conflicts`，预检直接失败（退出码 4）
   - 发放命令依赖已通过的预检报告，从源头阻止复制冲突文件

3. **回滚时目标文件被别人替换 -> 停止并说明**
   - 发放时为每个目标文件记录 SHA256
   - 回滚逐条计算实际 SHA256 与期望值比对
   - 不匹配时立即在该条目停止，打印路径 + 期望值 + 实际值
   - 必须显式加 `--force` 才会跳过校验

## 持久化文件结构

```
.exam_dispatch_state/
├── batches.json                 # 批次索引
├── events.log                   # 全局事件日志
└── batch-YYYYMMDD-HHMMSS-xxxxxx/
    ├── config_snapshot.json     # 配置 + CSV 路径快照
    ├── precheck_report.json     # 预检报告
    ├── dispatch_report.json     # 发放明细（含 SHA256）
    ├── rollback_report.json     # 回滚明细
    └── previews/                # 导入预演报告目录（同一批次可多次预演，不覆盖）
        ├── previews_index.json  # 预演索引
        └── preview-YYYYMMDD-HHMMSS-xxxxxx.json  # 单次预演完整报告
```
