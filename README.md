# 离线考试试卷包校验与发放 CLI (exam-dispatch)

一个面向离线考试场景的试卷包校验、批次发放、状态追溯、安全回滚命令行工具。

## 特性

- **结构化预检 (dry-run)**：科目/版本/人数/源文件路径/目标文件名多重校验
- **目标冲突检测**：复制前发现重复目标名立即报错终止
- **批次发放**：支持目录复制或 zip 打包两种模式
- **状态持久化**：批次状态、配置快照、预检/发放/回滚报告落盘，重启不丢失
- **安全回滚**：校验 SHA256，发现目标文件被替换时停止并给出证据
- **数据导出**：支持 JSON、批次 CSV、发放明细 CSV

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
exam-dispatch precheck  --config ... --rooms ...    # 第1步：预检
exam-dispatch dispatch  --batch-id ...              # 第2步：发放
exam-dispatch query     [--batch-id ...]            # 第3步：查询
exam-dispatch rollback  --batch-id ... [--force]    # 第4步：回滚
exam-dispatch export    --format ... --output ...   # 导出数据
```

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
详情 JSON 包含：配置快照、预检报告、发放明细（含 SHA256）、回滚记录。

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

## 数据导出

重新启动 CLI 后仍可导出所有历史数据：

```bash
# 全部数据 JSON（批次 + 事件日志）
python -m exam_paper_dispatcher.cli export --format json --output out/all.json

# 批次摘要 CSV
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
| 10 | IO_ERROR | 文件复制/删除等 I/O 错误 |
| 99 | UNKNOWN_ERROR | 未预期的异常 |

---

## 异常链路说明

1. **缺失试卷文件 → dry-run 不落已完成批次**
   - `precheck` 时如果发现 `missing_sources`，`report.passed = false`
   - 即使 `--persist`，批次状态只会写入 `failed`，绝不会出现 `completed`
   - 使用 `--no-persist` 时完全不写存储

2. **两个清单行指向同一目标名 → 复制前报冲突**
   - `precheck` 阶段对 `target_name` 做 group by
   - 任何重复都会进入 `target_conflicts`，预检直接失败（退出码 4）
   - 发放命令依赖已通过的预检报告，从源头阻止复制冲突文件

3. **回滚时目标文件被别人替换 → 停止并说明**
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
    └── rollback_report.json     # 回滚明细
```
