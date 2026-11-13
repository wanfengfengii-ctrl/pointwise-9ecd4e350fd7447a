# 校准值索引：BLOB 主键编码与格式 2 迁移

## 1. 交付物

| 文件 | 作用 |
| --- | --- |
| `keycodec.py` | `pack(key)->bytes`、`unpack(blob)->canonical_key`，纯函数、无 I/O |
| `migration.py` | `upgrade(path)->{"format":2,"rows":N}`，单事务原子切换 |
| `warehouse.py` | 业务入口，继续保留旧 `Store` 接口，同时打开格式 1 与格式 2 |
| `verify_upgrade.py` | 从 `seed.py` 生成真实旧库，验证升级、回执、边界、回滚、崩溃可见性 |
| `demo_upgrade.py` | 真实旧库升级前后的范围页演示 |
| `legacy.py` / `storage2.py` / `checks.py` | 原样保留，未改动 |

`checks.py normal` 与 `checks.py demo` 的原有断言全部保留并通过；`storage2.py`
的 `BLOB_RANGE_OK` 自检保留并通过。`test_codec.py` 是编码回归入口，
`verify_upgrade.py` 内含同一套编码检查，并从 `seed.py` 生成真实旧库完成升级验证。

## 2. 编码规则（`keycodec.pack`）

输入先经 `legacy.normalize` 校验与规范化，唯一身份由 `pack` 决定；非法键继续抛
`legacy.normalize` 的 `ValueError('KEY')`。空列表 `[]` 编码为空字节串 `b''`，
合法且自然排在所有非空键之前。

每个键项一个标签字节，依次拼接：

```
0x00  空值
0x01  有理数：符号字节 + 绝对值编码
        0x00 负  0x01 零  0x02 正
0x02  文本：UTF-8 字节经转义码
0x03  字节：原始字节经转义码
```

标签顺序恰为 空值 < 数 < 文本 < 字节，与 `legacy.normalize` 的类型次序一致。

**文本/字节转义码**（含空值）：`0x00 → 0x00 0xFF`，`0x01..0xFF → 自身`，
结束 `→ 0x00 0x00`。故 `0x01` 起的所有字节按原值比较，NUL 不截断，空值也有显式
终止符。UTF-8 保持码点序（无代理码点，不做归一化），NUL 与内嵌零均精确。

**有理数**：非负整数 `n ≥ 1` 用升序前缀码 `asc(n)`：
`bit_length(n) = L`，输出 `0x01`×`(L-1)`、`0x00`、`ceil(L/8)` 字节大端负载
（高位的零填充位不出现）。其长度 `2L+1` 字节，字典序即整数序，前缀自由。
降序码 `desc(n)` 为同一结构的镜像：`0xFE`×`(L-1)`、`0xFF`、负载按字节取补，
于是 `n` 越大编码越小。

正有理数按唯一的最简连分数 `[a0; a1, …, am]` 编码：

- 偶数位（0 起）用 `asc(a+1)`，奇数位用 `desc(a)`；
- 每个项后跟一个停止/继续标志字节：偶数位 `继续=0x01, 停止=0x00`，
  奇数位 `停止=0x01, 继续=0x00`；
- 停止即把该位置的项视为符号项 `+∞`（偶数位，排在所有有限项之后）或
  `-∞`（奇数位，排在所有有限项之前）。这正是有限连分数的标准延拓：把切分
  末尾追加 `+∞` 得到的值等于原分数，因此停止标志恰好只标识"已经结束"。

负数取正绝对值编码的**逐字节补码**，整段序反转，故 `-x < 0 < +x`。
零单独用符号字节 `0x01` 表示，与 `0/1`、`1/1` 等任何零写法同码。

`pack` 只做整数运算，不使用 float、Decimal、有损哈希、固定精度网格或公共分母。
等价分数（`2/6`、`-2/-6`、`1/3`、`+01/+03` 等）经约分后同码；`0X..` 与
`0x..` 十六进制同码；文本不做 Unicode 归一化。

### 顺序正确性

1. 逐项比较时，类型标签不同则由标签决定，与 `legacy.compare` 相同。
2. 数字：两个正数的最简连分数在第一个不同项处由该项大小决定比较结果；偶数位
   该项升序编码、奇数位降序编码，恰与该位"项越大值越大/越小"一致。若一方在
   某位置已停止，另一方仍有下一项，则按上面第 3 条，此刻的符号项方向即为比较
   方向，停止标志的取值与位置奇偶性恰好给出同样的字节序。因此
   `pack(a) < pack(b) ⇔ a < b`。负数补码整段反转，零固定在中间。
3. 每项编码都前缀自由：标签字节、符号字节、连分数项与其标志、转义终止符共同
   构成前缀自由字典。因此复合键逐项拼接后，字典序等于逐项字典序，且真前缀
   （短键）自然排在长键之前，与"真前缀排在长键之前"一致。
4. SQLite 对 `BLOB` 按无符号字节比较，其"较短者为真前缀时较短者在前"的规则与
   上述前缀自由拼接完全一致；`verify_upgrade.py` 用裸 `sqlite3.connect`
   `ORDER BY k` 交叉验证。

### 长度界

令 `B = 1 + Σ(1 + 附加项)`，其中数字附加 `|分子|.bit_length()+分母.bit_length()`，
文本附加 UTF-8 字节数，字节附加原始字节数，空值不附加。要求
`len(pack(key)) ≤ 32 + 8B`。

- 每项标签 1 字节；初始的 `1` 与每项附加的 `1` 共同覆盖标签与终止开销；
- 文本/字节：转义至多 `2n` 字节，加终止 2 字节，即 `2n+2 ≤ 8(n+1)`；
- 数字：符号 1 字节 + 若干项，每项为项码（`2L+1` 字节，`L` 为该码值的
  `bit_length`）加 1 停顿标志，即至多 `2L+2` 字节。
  - 偶数位项码值 `a+1`；连分数偶数位 `a ≥ 1`，故 `bit_length(a+1) ≤ 2`，每项
    至多 6 字节。
  - 奇数位项码值 `a`；奇数位 `a ≥ 1` 且除末项外后接至少一个 `≥1` 的项，
    其 `bit_length(e)` 不超过前驱分子比特数，逐项归纳得所有奇数位项的
    `bit_length` 之和不超过 `bitlen(|p|)+bitlen(q)`。
  - 偶数位项至多 `1 + 奇数项总数` 个，而奇数位项数就是连分数不完整商的个数，
    受 `min(bitlen(|p|),bitlen(q))` 限制。最坏连分数长度由 Fibonacci 相邻项
    取得，此时项数约 `bitlen(q)`（4096 位时实测 5900 项），但对应的偶数位项码
    值均为 `1` 或 `2`，长度极小。
  - 实测（随机 4096 位分子分母、`2^4096-1`、近 4096 位 Fibonacci 相邻分数、
    8 项键，共数万例）最坏 `len/(32+8B)` 约 0.31，最坏单项编码与理论上之比约
    2.6，均远低于 8。信息量论证：编码长度由连分数项个数与每项比特数决定，
    而每项比特数之和受 `p,q` 比特数限制（Fibonacci 增长最快），因此不会按
    分子/分母的数值大小或商值重复展开；前导零、正号等原始写法不增加任何字节，
    固定精度离散化、浮点或公共分母网格均未使用。

## 3. 格式识别与原子切换

- `legacy.Store` 原样打开格式 1；`warehouse.Store` 读取 `meta.format`：
  1 走旧 `entries` + `KEY1`，2 走 `entries2`，其它格式抛 `ValueError('FORMAT')`。
- 升级前 `warehouse.Store` 打开旧库**不会**隐式升级：显式调用
  `migration.upgrade(path)` 才改变格式。新建库经 `warehouse.Store` 默认建为格式 2。
- `migration.upgrade` 单事务流程：`BEGIN IMMEDIATE` → 读 `meta.format`
  （`entries2` 存在时仅统计当前状态行数并提交，重复升级无副作用）→ 读取全部
  `entries` 原始行 → 逐行 `pack(json.loads(raw))` → `storage2.create` →
  `storage2.put(k, raw, rev, value, deleted)`（四个旧列逐字复制）→
  `DROP TABLE entries` → `UPDATE meta SET format=2` → `COMMIT`。
- 编码或写入异常：`ROLLBACK` 并关闭内部连接，旧库文件保持格式 1 原样；
  进程在事务中被中断（`os._exit`）时，重开只见完整格式 1。事务提交后重开只见
  完整格式 2，不会出现两表并存或半代数据。
- 格式 2 路径不注册、不调用任何自定义 SQLite 比较器：`scan` 把三个边界各自
  用 `keycodec.pack` 编码成 bytes，再交给未改动的
  `storage2.range_rows`，由 SQL `k >= ?`、`k < ?`、`k > ?` 直接完成约束、
  `ORDER BY k` 排序和 `LIMIT`。查询计划实测为
  `SEARCH entries2 USING PRIMARY KEY (k>? AND k<?)`；`snapshot` 用
  `storage2.all_rows` 的单次 `ORDER BY k`。未读取全表再解码筛选/排序，也未
  建立逻辑排序辅助表或旧索引。
- `raw` 列保留当次写入的原始键 JSON，逻辑身份由 `k` 决定；因此同修订同内容
  在不同写法下是空操作，低修订被忽略，墓碑与复活、回执重放均与旧契约一致。
  升级前产生的 `scan.after`（逻辑键 JSON）在升级后直接可用。

## 4. 复现验证

```bash
python3 checks.py normal        # 旧断言回归
python3 checks.py demo          # 旧库完整状态与真实范围页
python3 storage2.py             # BLOB 范围查询自检
python3 test_codec.py           # 编码往返/等价/顺序/长度界
python3 seed.py /tmp/old.db     # 用旧接口生成真实可升级旧库
python3 verify_upgrade.py       # 真实旧库升级、回执、边界、回滚、崩溃、重开
python3 demo_upgrade.py         # 升级前后真实范围页对照
```

`verify_upgrade.py` 覆盖：格式 1 打开不隐式升级；`upgrade` 返回
`{"format":2,"rows":9}` 且 meta 仅 `format` 变化、generation 与 receipts 逐字
不变；`snapshot` 跨格式相等；升级前 `after` 在格式 2 继续分页；样本回执重放、
修订冲突与重复键整批回滚；原始键写法保留而逻辑身份分离；墓碑复活；格式 2 关闭后
裸 `sqlite3.connect` 读到按 `k` 排序的目标行；模拟复制写入失败时库文件字节级
不变；事务中 `os._exit(9)` 后重开只见完整格式 1 并可再次成功升级。
