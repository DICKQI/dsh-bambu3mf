# 拓竹 3MF 编辑器（`@local/dsh-bambu-3mf`）

在 DeepSeekRuntime 里直接检查、对比、建模、诊断、修改、渲染并切片 Bambu Studio 的 `.3mf` 工程文件。插件注册 8 个模型工具，底层是一个只管"动刀"的 Python 内核：**只重写真正改动的 zip 条目，其余条目逐字节保留**，因此打印参数、盘面布局、缩略图和 Production Extension 结构不会被 round-trip 破坏。

## 八个工具

| 工具 | 作用 |
|---|---|
| `bambu3mf_inspect` | 只读检查：盘/对象/世界包围盒/摆放/耗材/打印机与工艺/结构诊断；`detail=full` 还会列出矩阵、parts、连通壳、共享网格引用、包内条目与项目设置 |
| `bambu3mf_edit` | 原子编辑：移动、定位（`z_mode: "bed"` 可直接指定底面高度）、绕 Z 旋转、复制、删除、重命名、换挤出机、换盘、落床、printable、对象设置 |
| `bambu3mf_mesh` | 单对象网格：烘焙摆放、缩放、旋转、镜像、平移、**按区间拉伸（`stretch`）**、焊接、清理、平面切割+封盖、STL 导入/导出；**`diagnose` 拓扑/绕向/退化/薄片/共面内部面**、**`section` 剖面轮廓**、**`measure` 高度场/材料区间**；`components` 列出连通体，`select` 可只对某个连通体或包围盒做变换 |
| `bambu3mf_build` | **构造建模**：`boolean`（union/subtract/intersect，操作数可以是对象、STL 或 box/cylinder/sphere 基本体）、`primitive`、`split`（一个平面拆成两个独立对象）、`replace`（一步换掉几何）。世界毫米坐标，保留目标 id/名称/盘/挤出机 |
| `bambu3mf_render` | 出 PNG 预览：`iso` / `top` / `front` / `right` / `plate`；`framing=objects|bbox` 自动取景、`zoom`、`camera`、`crop`，小零件也能看清 |
| `bambu3mf_settings` | 读写 `project_settings.config`（层高、墙数、填充、支撑、耗材颜色…）与对象设置 |
| `bambu3mf_slice` | 调本机 Bambu Studio CLI 切片，导出 `.gcode.3mf` / `.gcode`，是"没改坏"的最终证据；结果带 **Bambu 自己算的分特征耗时**（外墙/内墙/填充/空驶…），围绕切片会附一份对象间距报告（建议式，不拦切片） |
| `bambu3mf_diff` | 只读对比两个 3MF：`project_settings` 逐键、对象（名称/盘/挤出机/面数/包围盒/位置）、盘归属、切片结果；`keys` 可只查指定键；**`geometry=true` 做三角面级对比**（新增/删除/翻转面、体积/bbox 差值、差异集中区） |

## 典型工作流

```
inspect → build / edit / mesh / settings → render 看图 → slice 验证 → 交付新文件
```

1. 先 `bambu3mf_inspect` 看清对象 id、盘归属和尺寸；
2. 摆放类改动用 `bambu3mf_edit`（默认输出 `<原名>_edited.3mf`，输入不动）；
3. 需要挖/并/拆/换几何时用 `bambu3mf_build`，单对象变换用 `bambu3mf_mesh`；
4. `bambu3mf_render` 出图确认（小零件用 `framing=objects`）；
5. `bambu3mf_slice` 真切片，确认 Bambu Studio 仍然接受这个包。

### 只改一个零件（例如"把中间轴缩小一点"）

一个 object 里常常塞着好几个互不相连的壳（外圈 + 中心轴 + 卡子 + 顶盖）。先摸清壳，再只动目标壳：

```jsonc
// 1) 看壳：也可以直接 inspect detail=full，返回里带 components
{"cmd":"mesh","args":{"file":"…3mf","object":12,"op":"components"}}
// → #0 11552 面 206.0×206.0×62.0 / #1 11540 面 70.39×55.29×69.5 / #2 … / #3 …

// 2) 只把中心轴和它的顶盖缩到 95%，其余几何一毫米不动
{"cmd":"mesh","args":{"file":"…3mf","object":12,"op":"scale",
                      "scale":[0.95,0.95,1.0],"select":{"component":[1,3]}}}

// 3) 切片确认
```

`select` 支持 `{"component": 0 | [0,2]}`（连通体索引，`0` 是面数最多的那个）和
`{"bbox": {"min":[x,y,z], "max":[x,y,z]}}`（按三角形重心落在世界坐标盒内筛选）。
带 `select` 时 `pivot` 默认取**选区自己的包围盒中心**，所以缩放/旋转天然围着这个零件自己的轴心。

> 用 `bbox` 选区时，如果选区边界横穿一个连续的壳，那块表面必然被切开：变换后开放边会增加。
> 结果里的 `selection.seamEdges` 与摘要会明确报出"选区内部接缝 N 条边"。要整体搬动某个壳，
> 请用 `select.component`；要在中间加长而不动两端，请用 `stretch`（见下）。

### 把零件变长而不动两端（`op: "stretch"`）

只改顶点坐标、不切不补：区间内按比例拉长，区间外的几何整体刚性平移，**三角形数量与拓扑完全不变**，
所以两端形状、卡爪配合尺寸、圆角、壁厚全部保持原样。

```jsonc
// 内腔跨距 35.2 → 52.8 mm（150%）：中间 ±14 的直段拉长 17.6，两端各外移 8.8
{"cmd":"mesh","args":{"file":"…3mf","object":2,"op":"stretch",
                      "axis":"x","band":[148.4,176.4],"delta":17.6,
                      "negative_share":0.5}}
```

- `band: [low, high]` 是**世界坐标**下要拉长的那一段；`delta` 是该轴上增加的长度（负数=压缩）。
- band 必须落在物体该轴的范围内：完全落在外面会直接报错（否则只会把整件挪走、尺寸不变），越界也会报错（实际增长量会小于 `delta`）。报错信息里带上该轴的真实范围。
- `negative_share` 决定增量分给低侧的比例：`0.5` 两端对称（默认），`0` 只朝 +轴方向长。
- 边界恰好落在已有面上时会多一句提醒（`coincidentWithFace`），接缝处会有折痕，但结果仍可用。
- 相比 `scale`：`scale` 会把壁厚、卡爪一起放大；`stretch` 只动长度。

### 在插件里真正建模：`bambu3mf_build`

上一轮"把挂钩挖掉、搬到中间、再并回去"必须在外部 Python 里做布尔；0.5.0 起插件自己就能做。
所有坐标都是**世界毫米**，目标对象的 id/名称/盘/挤出机保持不变；几何写回后 `transformBaked=true`
（世界位置不变，对象自身的旋转/缩放被烘焙进顶点）。

```jsonc
// 从对象 2 上挖掉一个世界坐标 box（例如切掉旧挂钩柱）
{"cmd":"build","args":{"file":"…3mf","ops":[
  {"op":"boolean","object":2,"operation":"subtract",
   "with":{"primitive":{"shape":"box","size":[5.2,30,36],"center":[108,128,18]}}}
]}}

// 把一段圆柱并回背板（必须产生真实体积重叠，不能只贴面）
{"cmd":"build","args":{"file":"…3mf","ops":[
  {"op":"boolean","object":2,"operation":"union",
   "with":{"primitive":{"shape":"cylinder","radius":2.4,"height":36,
                        "center":[90,132,18]}}, "merge_parts":true}
]}}

// 一个平面拆成两个独立对象，并分开 0.5 mm
{"cmd":"build","args":{"file":"…3mf","ops":[
  {"op":"split","object":2,"plane":{"axis":"x","value":128},
   "names":["CardBrickH_L","CardBrickH_R"],"gap":0.5}
]}}

// 一步替换几何（不再 import + delete + weld）
{"cmd":"build","args":{"file":"…3mf","ops":[
  {"op":"replace","object":2,"source":{"stl":"H:/tmp/new.stl"}}
]}}
```

- `boolean` 的 `with` 支持 `{object}` / `{stl, translate?}` / `{primitive}`；`keep_operand` 可把操作数也留成对象。
- `repair` 默认 `weld`（焊接 + 去退化/重复面）；目标或操作数不闭合、绕向不一致会直接拒绝，不会写出半成品。
- **`BOOLEAN_TOUCH_ONLY`**：两个实体只是贴面（交集体积 0）。这正是"挂钩看起来并上了、Bambu 却当成两个部件"的根因；
  让操作数嵌入目标 1–2 mm 再并集。
- **`BOOLEAN_DISCONNECTED`**：结果有多个互不连通的壳，Bambu 可能拆件；`mesh op=diagnose` 可进一步看共面内部面。
- `split` 默认删除原对象；`keep_original=true` 用于对比。`gap=0` 时两半切面重合，同盘打印可能触发重叠告警。

### 诊断、剖面与测量（`mesh` 的三个只读 op）

```jsonc
// 拓扑事实：壳/绕向/体积是否可靠/退化面/薄片/共面内部面
{"cmd":"mesh","args":{"file":"…3mf","object":2,"op":"diagnose"}}

// 某个 Z 高度的剖面：2D 环、净面积、周长；format=svg 可落盘
{"cmd":"mesh","args":{"file":"…3mf","object":2,"op":"section",
                      "plane":{"axis":"z","value":18},"format":"svg",
                      "out":"H:/tmp/section.svg"}}

// 某块区域的高度场（找 0.4 mm 台阶最快）；也可 mode=spans / extent
{"cmd":"mesh","args":{"file":"…3mf","object":2,"op":"measure",
                      "bbox":[105,128,0,110,140,36],"axis":"z",
                      "mode":"heightmap","grid":64,
                      "out":"H:/tmp/height.png"}}
```

`analyze` 现在把"水密"和"体积可靠"分开：三角形绕向不一致时，`volumeMm3` 返回 `null`
并报 `INCONSISTENT_WINDING`，不再给出 1.7 / 15.9 cm³ 这种由有符号体积互相抵消出来的假数字。
`weld` / `cleanup` / `analyze` / `validate` 共用同一套焊接后的边统计，不会再出现
"analyze 说水密、weld 说 3 条开放边"。

### 放大看细节：`render framing`

```jsonc
// 只按对象定标，40 mm 的零件也能占满画面；camera 可指定方位角/仰角
{"cmd":"render","args":{"file":"…3mf","views":["iso","front"],"objects":[2],
                        "framing":"objects","zoom":1.4,
                        "camera":{"azimuth":35,"elevation":25}}}

// 只看某个世界坐标盒，盒外三角形裁掉
{"cmd":"render","args":{"file":"…3mf","framing":"bbox",
                        "bbox":[105,128,0,110,140,36],"crop":true}}
```

`scene`（默认）保持旧行为（对象 + 整张床）；`objects` 忽略床面；
标签现在写实际绘制/总对象数（`1/1 objects`），过滤后不再误写全对象数。

### 两份工程差在哪几个面：`diff geometry=true`

```jsonc
{"cmd":"diff","args":{"file":"v2.3mf","against":"v3.3mf","geometry":true}}
```

按 0.001 mm 量化顶点做三角面集合对比，输出新增/删除/翻转面数、体积与 bbox 差值，
并把差异三角形按 5 mm 网格聚成"差异集中区"（例如 `X 105.4–105.9, Z 33.3–36.0`）。
对象 id 重排不影响匹配；超过 `geometry_limit`（默认 200 万面）的对象会跳过并说明。

### 间距报告（建议式，不拦切片）

切片前后都会跑一次 `preflight`，但它**只报告、不阻断**——这一点是实测定的：Bambu 能切间距 5 mm 的排布，
也能切包围盒互相穿插的装配体（`A1外挂密封盒` 盘 2 的最近曲面 3.2 mm）。所以"间距阈值"不能当闸门，
0.3.0 曾用它拒切可切工程。

- 每盘一行：最紧的两个对象相距多远（包围盒间距 + 最近曲面距离），仅供判断；
- `OBJECT_OVERLAP`：包围盒确实重叠的对象对，附三轴穿透量与最近曲面距离（装配体常有，属正常）；
- **只有真实表面接触（最近曲面 ≈ 0）才算不通过**，并给出可直接套用的 `move` 增量与目标世界坐标；
- 算不出空位的对象会明确列在 `infeasible` 里，而不是悄悄漏掉。

```jsonc
// 单独体检
{"cmd":"preflight","args":{"file":"…3mf","plate":1}}
// 让包围盒重叠也阻断（可选，默认不阻断）
{"cmd":"preflight","args":{"file":"…3mf","strict":true}}
```

切片工具的参数：省略 = 建议式；`"strict"` = 预检不通过就中止；`false` = 不做预检。
切片失败时，错误信息里会附上这份报告——CLI 只会回一句 “G-code conflicts detected”，
真正能对应到具体对象对的是这份本地测量。

## 打印时长分析

四个入口，按"从快到慢"排列：

| 想回答 | 用什么 | 成本 |
|---|---|---|
| 这个已切好的包要打多久、多少料？ | `inspect` 读 `Metadata/slice_info.config` | 0.05 s |
| 时间花在哪一类动作上？ | `slice` 结果的 `featureTimes`（Bambu 自己算的分特征秒数） | 一次切片 |
| 两个版本到底哪里不同？ | `diff` | 两次 inspect |
| 还没切，几何上有什么会拖慢打印？ | `mesh op=analyze` | 53 万面约 5 s |

### 切片包：`inspect` 直接给结论

已切片的 `.gcode.3mf` 里有 Bambu 写的 `Metadata/slice_info.config`，`inspect` 现在会读它：

```jsonc
{"cmd":"inspect","args":{"file":"…gcode.3mf"}}
// 已切片（Bambu 02.09.01.70）：盘 1 · 预估 34.4 分钟 · 6.15 g · 耗材 #1 PLA #161616 2.05 m/6.15 g
```

结构化返回里是 `sliceInfo.plates[]`（`predictionSeconds` / `weightGrams` / `filaments[].usedMeters` / `objects` / `warnings`）。
预估时长取 `prediction`，与 Bambu Studio 显示一致；`slice` 工具的 `estimateMinutes` 也改成同一口径（`total_predication`）。

### 切片结果：分特征耗时

`bambu3mf_slice` 的 `sliceStats.featureTimes` 是每类特征的秒数（Bambu 自己算的），文本摘要会给一行分解：

```
耗时分解（特征合计 34.7 分钟）：外墙 27.7 分 80% · 自定义 6.1 分 18% · 冲刷 0.4 分 1% · 空驶 0.4 分 1%
```

百分比按**特征合计**算，不是按总时长（两者差 1%~10%，见 `featureTotalSeconds`）。
`plateStats` 给出每盘的 `totalMinutes` / `mainMinutes` / `sliceSeconds` / `exportSeconds`——后两个是毫秒换算来的切片进程耗时，
用来看"为什么切片慢"（导出 G-code 常是最大头）。空驶占比 ≥8% 时会多一行提示：孔洞/碎件多会拉长这段。

### 网格：`mesh op=analyze`（不用切片）

```jsonc
{"cmd":"mesh","args":{"file":"…3mf","object":2,"op":"analyze"}}
// 对象 2 · 可打印性：536984 面 · 1 个连通壳 · 体积 344.3 cm³ · 表面积 2374.9 cm²
// 尺寸 254×254×103 mm（515 层 @ 0.2 mm）
// 水密 · 通孔 1177 个（genus）
// 悬垂 45°：375.6 cm²（15.8%），贴床 368.2 cm²，需支撑 7.4 cm²（0.3%）
```

- **通孔数 = 拓扑 genus**：每个贯穿孔给闭合曲面加一个把手，所以 `genus` 就是通孔数；不水密时返回 `null`，不给假数。
- **悬垂扣掉贴床面**：不扣的话任何零件都会报出整块底面（那部分由床板托着，不需要支撑）。
- `overhang_angle` 可改阈值（默认 45°）。这里只有几何事实，没有时间预估——时间只信切片器。

### 两份工程：`diff`

```jsonc
{"cmd":"diff","args":{"file":"A.3mf","against":"B.3mf"}}
// 设置：558 项中 2 项不同（2 项值不同）
//   ~ layer_height: 0.2 → 0.16
// 对象：8 ↔ 8；0 个仅左 / 0 个仅右 / 1 个有差异
//   #4 16L+R：包围盒 […] → […]；位置 […] → […]
// 盘：5 ↔ 5；0 个仅左 / 0 个仅右 / 0 个有差异
// 切片：左未切片；右 盘 1 34.4 分钟 / 6.15 g
```

`keys` 只比指定键，并**同时列出相同的键**（回答"这个键一样吗"）：

```jsonc
{"cmd":"diff","args":{"file":"A.3mf","against":"B.3mf","keys":["layer_height","wall_loops"]}}
// 设置（匹配 2 项）：1 项不同，1 项相同
//   ~ layer_height: 0.2 → 0.16
//   = wall_loops: 2
```

数值字符串按数值比（`0.2` 与 `"0.2"` 视为相同），避免"类型变了"的假差异；对象按 id 配对，id 不同就报 added/removed。

## 关键保证

- **不 round-trip**：不把 3MF 读成 mesh 再整体导出；未改动的条目按原 `ZipInfo`（顺序、压缩方式、时间戳）复制，写后逐条校验 SHA-256。
- **原子写**：先写临时文件再 `os.replace`；校验失败自动回滚删除产物。
- **输入保护**：默认只写新文件；`in_place: true` 时先写 `<名>.bak-<时间戳>.3mf` 备份。
- **结构校验**：写前写后都校验引用完整性（component 路径/对象 id、build 项、盘归属、UUID 唯一、三角索引、网格指数），只报告"新增"的问题；`ASSEMBLE_STALE_OBJECT`（`<assemble>` 还引用已删对象）和 `DANGLING_RELATIONSHIP`（`3dmodel.model.rels` 指向不存在的条目）会让 Bambu 直接报 "The input model file to the slicer can not be parsed"，因此按 error 处理并阻止写出。
- **删除干净**：`delete` 同时清理 `<object>`、`<model_instance>`、`<build><item>`、`<assemble_item>`（instance 与 volume 两条）以及该对象独占的 `3D/Objects/*.model` 与它在 `3dmodel.model.rels` 里的关系；重接线/几何重写遗留的旧对象文件也走同一套清理。
- **子网格编辑不撕裂**：`select` 命中部分会被复制一份独立顶点后再变换，未选中的壳顶点坐标完全不变（不会因为共用顶点被拖走）。若选区边界横穿连续曲面（`bbox` 选区常见），`selection.seamEdges` 会报出会裂开的边数，而不是悄悄产出破面。
- **共享网格安全**：多个根对象共用一份网格时，几何操作会先克隆对象文件并分配包内唯一的嵌入对象 id（Bambu 的加载器按该 id 全局索引，重复会导致切片失败）。**只有目标对象的几何会变，兄弟对象的几何保持原样**，结果里 `detachedMesh` 与摘要会说明"已单独复制一份，其余对象保持原样"。
- **布尔是原子的**：目标/操作数先焊接并检查闭合与绕向，再交给 manifold3d；结果不是闭合实体时按 error 回滚，不写出文件。`BOOLEAN_TOUCH_ONLY` / `BOOLEAN_DISCONNECTED` 会在结果里明确报告"只是贴面"或"多壳"，而不是让 Bambu 切片时才发现。
- **拓扑口径统一**：`mesh_quality` 先焊接顶点再统计边；`analyze`、`weld`、`cleanup`、`validate(deep)` 与 `mesh op=diagnose` 对同一网格给出一致的水密/绕向/体积结论。绕向不一致时体积为 `null`，不会用有符号体积互相抵消后的假数字。
- **新建对象不串号**：同一调用里连续创建多个对象（`split` 的两半、`primitive`）时，路径与嵌入 id 会看到前面刚创建的文件；两个根对象不会指向同一份网格。
- **取景不改几何**：`render framing` 只影响相机/画布，不写回 3MF；`bbox + crop` 只是丢弃画面外的三角形，模型本身不变。
- **自动避让**：`set_plate` 与零偏移的 `duplicate` 会和目标盘上已有对象做包围盒避让；放不下时报错而不是写出会撞车的布局。
- **对象间距是报告而非闸门**：`validate` 按盘比较世界包围盒，每盘给一行"最紧的一对相距多远"（`PLATE_CLEARANCE`），并对包围盒重叠的对象对报 `OBJECT_OVERLAP`。这里没有距离阈值——实测 Bambu 能切间距 5 mm、甚至有互锁零件的工程，阈值化只会误拒。`preflight` 仅把"真实表面接触"判为不通过，`preflight: "strict"` 才把包围盒重叠也算不通过。
- **切片即验收**：`bambu3mf_slice` 读 Bambu 自己写的 `result.json`（返回码、每盘对象包围盒、耗材、预估时间、分特征耗时），退出码为 0 不代表成功，二者都会检查。失败时不只回 CLI 那句话，还会附上预检结论。
- **切片数据必须是本次的**：输出目录跨次复用，所以 `result.json` 与导出包都会校验 mtime；上次的同名产物不再算"本次成功"，`result.json` 过期时时间数据整体忽略并报 `STALE_RESULT`，不拿旧数字冒充新结果。

## 按需加载（门控）

八个工具全部注册时，定义合计约 **6,900 token**，会随每次请求一起发送。绝大多数会话根本不碰 3MF，
所以这些定义默认**不进入上下文**：`lib/gate.mjs` 在**每个模型步**用
`register(definition, { when })` 判定一次，判定为否时工具会同时从工具描述、PTC 绑定和派发中消失。

判定按“便宜→贵”短路，命中任一条即放行：

| # | 信号 | 放行范围 |
|---|---|---|
| 1 | 会话工作目录**顶层**有 `*.3mf` / `*.gcode.3mf` | 全部 8 个（`slice` 另需 Bambu Studio 可解析） |
| 2 | 工作目录顶层只有 `*.stl` | 仅 `bambu3mf_mesh`（它是唯一读 STL 的工具） |
| 3 | 本会话**最近 20 条**用户消息里出现过 `.3mf` 路径 | 仅轻量入口：`inspect` / `render` / `diff` |
| 4 | 本会话已成功调用过任一 `bambu3mf_*` | 全部 8 个（会话内粘住，换目录也不撤销） |

由此得到的开销：

- **完全无关的会话**：0 token
- **只是提到过 3mf**：约 1,300 token（三个轻量工具）
- **真的在 3mf 项目目录里干活**：约 6,900 token（八个工具，含 build）

两条实现约束（改动时请保留）：

1. **条件绝不抛异常。** `when` 抛错会让整步准备失败（`TOOL_ACTIVATION_FAILED`），
   而不只是隐藏一个工具。所以文件系统与会话读取全部包在 try/catch 里，失败即视为“不可见”。
2. **条件必须便宜。** 它跑在每步的 pre-step 路径上，所以只做一次非递归 `readdirSync`
   （结果按目录缓存 5 秒、上限 32 条），**绝不 spawn 进程**——探测 Python 运行时一次约 175ms。

工作目录取自 `workingDirectory` 服务的实时值（`ctx.get('workingDirectory').get(session)`），
所以 `working_directory cd` 会被立刻看到；`agent.session.header.cwd` 只是兜底，
它是 Session 创建时固定下来的，不跟随 `cd`。目录扫描是**顶层**的，3mf 放在子目录里不会触发
（递归扫描大目录会拖慢每一步）。遇到这种情况，要么 `working_directory cd` 进去，
要么在消息里直接点名那个 3mf 文件（触发第 3 条）。

第 3 条的窗口按**用户消息条数**计，不按事件数：一次工具调用会追加约 5 个事件，
按事件计会在几次调用后就悄悄失效。`source.kind !== 'user'` 的消息（`runtime-context` 快照、
`skill-catalog`）不参与扫描——它们每轮重述整份环境，只会制造假阳性。
另外，一轮的**第一步**在 `when` 求值时当轮那条用户消息还没写进 log
（`preStep` 先 `claim()`、后 `append`），所以扫描同时读 `agent/inbox/spliced` 的 `inserted` 载荷并按 `id` 去重。

验证：`node selftest/gating.mjs`（36 项，离线，不需要设备或 Python）。

## 配置

`cordis.patch.yml` 里可改：

| 键 | 默认 | 说明 |
|---|---|---|
| `pythonPath` | 自动 | 指向带 lxml / numpy / Pillow 的 `python.exe`；布尔建模另需 `manifold3d`。自动搜索 `$DSH_HOME/dsh-runtimes/*/dependencies/python/python.exe`，再退回 PATH |
| `bambuStudioPath` | 自动 | 指向 `bambu-studio.exe`；默认找 `C:\Program Files\Bambu Studio\` 等常见位置 |
| `renderWidth` | 900 | 预览宽度 |
| `maxTriangles` | 250000 | 单视图三角面上限，超出按步长抽样并标注 |
| `sliceTimeoutMs` | 1800000 | 单次切片硬超时 |

## 目录结构

```
index.js                 宿主半边：注册工具（零外部 import，便于 link 安装）
lib/bridge.mjs           Python 进程桥：JSON stdin/stdout、超时、取消、错误码
lib/gate.mjs             每步门控：按工作目录与会话历史决定八个工具是否进入上下文
lib/tools.mjs            8 个 ToolDefinition：入参 schema、输出 schema、卡片与图片
lib/slice.mjs            Bambu Studio CLI 调用、间距预检与结果解析
locale/zh.json, en.json  工具卡片标题与描述的本地化文案
python/threemf/package.py    zip/OPC 读写，条目保真
python/threemf/model.py      根模型 / 对象文件 / 变换矩阵 / 网格
python/threemf/settings.py   model_settings、project_settings、盘原点
python/threemf/csg.py        manifold3d 布尔封装（可选依赖）
python/threemf/geometry.py   焊接、清理、拓扑/绕向/退化诊断、基本体、剖面/测量、区间拉伸、平面切割+封盖、连通体分析、STL
python/threemf/render.py     Pillow 软件光栅化预览
python/threemf/validate.py   结构诊断 + 对象间距诊断
python/threemf/scene.py      加载、编辑、保存、校验
python/threemf/cli.py        JSON 命令入口（含 preflight）
python/threemf/slicing.py    slice_info.config 解析（切片包的真实时长/耗材）
python/threemf/compare.py    两份 3MF 的逐键对比与 diff 文本
selftest/run_selftest.py     对样本副本跑端到端断言（含合成切片包、genus/悬垂、diff）
selftest/gating.mjs          门控单测（36 项，离线，不需要设备或 Python）
selftest/make_fixtures*.py   生成 output-schema 契约样本（v05 起含建模/诊断）
selftest/schema_contract.mjs 用工具自己声明的 schema 校验真实内核输出
selftest/slice_result.mjs    result.json 解析 + 切片输出 schema 契约
selftest/data/               契约测试用的固定样本（如合成切片 result.json）
```

## 已知限制

- 布尔运算依赖 `manifold3d`（可选安装）。未安装时 `boolean` 返回 `BOOLEAN_ENGINE_MISSING` 并给出安装命令；基本体/拆分/替换/剖面/测量/渲染不受影响。
- `split` 默认删除原对象；`gap=0` 时两半切面重合，同盘打印可能触发重叠告警。`boolean`/`replace` 会把目标对象的旋转/缩放烘焙进顶点（世界位置不变），结果里 `transformBaked=true`。
- `mesh op=diagnose` 的共面内部面检测是深检：按平面分组、组上限 300 个面、最多检查 20 万对；超出会标记 `coincidentTruncated`，不保证穷尽。
- `mesh op=measure` 的 `spans` 基于三角形在该轴的投影区间，是**定位用**的近似，不是实体内外判定；需要精确表面高度用 `heightmap`。
- 只处理未加密的 ZIP/OPC 3MF；已切片的 `.gcode.3mf` 只能 inspect，几何编辑会被拒绝（`SLICED_FILE`）。
- `select` 只配合 `scale` / `rotate` / `mirror` / `translate`；`weld` / `cleanup` / `plane_cut` / `flip` / `stretch` 仍是整块网格语义（传 `select` 会明确报错，不会静默忽略）。
- `stretch` 是仿射重映射而非重建：区间边界若与已有面共面，接缝处会留下折痕（结果里有 `coincidentWithFace` 提示）；它也不会自动补加强筋——原作者做加长版时手工补了加强筋，本工具不会。
- 连通体按"焊接后顶点相连"判定，只共点/相邻但未合并顶点的壳会被算作不同连通体；一个对象里壳非常多时索引以面数降序（0 最大），可用 `bbox` 选区代替索引。
- 不处理多色/纹理语义：未改动三角面的颜色属性原样保留，切割新增的封盖面不带颜色。
- 不做自动摆盘、自动定向、支撑生成——这些交给 Bambu Studio。
- 缩略图（`Metadata/plate_*.png`、`top_*`、`pick_*`）在编辑后不会重绘，可能显示旧画面；Bambu Studio 保存时会重建。`plate_N.json` 的包围盒会同步。
- 平面切割恰好落在共面几何上时会自动偏移 0.001 mm 重试；个别极端轮廓会使用兜底三角化（结果里会标注 `capFallback`）。
- 渲染是"看得对"级别的预览（正交 + 平面着色），不是照片级渲染。
- `mesh op=analyze` 是**几何事实**，不是时间预估：时间只有切片器能算。genus 统计的是拓扑把手数（一个贯穿孔 +1），盲孔/凹槽不加；网格不水密时 genus 返回 `null`。
- analyze 不计算"最小筋宽/壁厚"——没有既便宜又可靠的定义，宁可不算也不给会误导的数字。悬垂默认按 45° 判、距床面 0.2 mm 内的面视为贴床（由床板支撑）。
- `diff` 按对象 id 配对：两份工程若各自导出、id 重排，会显示成 added/removed 而不是"同一个零件"。设置值按语义比较（`0.2` 与 `"0.2"` 视为相同），但不判断某个值对该打印机是否合法。

## 自测

```powershell
$PY = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
& $PY "H:\dsh_workspace\dsh-bambu-3mf\selftest\run_selftest.py" --samples H:\bambu --skip-slice
node selftest/gating.mjs
& $PY selftest/make_fixtures_v05.py --file H:/bambu/CardBrickH.3mf
node selftest/schema_contract.mjs selftest/fixtures_v05
```

覆盖：样本 inspect、**切片包直接报时长/耗材（合成 `.gcode.3mf`）**、**genus/悬垂/层数（cube + 环面 + 空网格）**、
**布尔加减并/三轴体积与单壳**、**拆分两半/封盖/gap/原对象删除**、**替换几何保持对象身份**、**基本体**、
**绕向不一致时体积为 null**、**共面内部面检测**、**剖面面积/高度场/材料区间**、**几何 diff 聚类**、
**两份工程 diff（设置/对象/keys 过滤/错误键）**、**result.json 解析与旧产物守卫（node）**、未改动条目哈希不变、移动/烘焙/缩放/切割封盖、区间拉伸（拓扑不变 + 真切片）、
真实接触才阻断而间距不阻断、可切工程不被拦（`监控支架` / `特氟龙管` 盘 2）、盘 2 建议坐标落在本盘内、
空网格不崩、band 越界被拒、`z_mode=bed` 底面定位、选区接缝告警、设置写回、渲染产物（含 `framing=objects`）、STL 往返、
编辑后切片（含移动对象坐标断言）、切割后切片、删除后包仍可打开（assemble/关系无残留 + 真切片）、
连通体列表、只缩放指定连通体（其余不动）并切片、Core 3MF、已切片文件拒绝、满盘时诊断噪声与耗时、
**JS output-schema 契约校验**（`selftest/schema_contract.mjs` + `selftest/make_fixtures.py` / `selftest/make_fixtures_v05.py`），
源文件哈希不变。加 `--skip-slice` 可跳过多轮真实切片。

## 变更记录

- **0.5.0**（把"重新建模"收回插件，并按需加载）
  - **新工具 `bambu3mf_build`**：`boolean`（union/subtract/intersect，操作数为对象/STL/基本体）、`primitive`、`split`（一个平面拆成两个独立对象）、`replace`（一步换几何）。世界毫米坐标，保留目标 id/名称/盘/挤出机；`partsCollapsed`/`detachedMesh`/`transformBaked` 明确报告结构变化。
  - **布尔内核**：新增 `python/threemf/csg.py`，用 `manifold3d==3.5.4`（Apache-2.0）。未安装时只有布尔失败，错误里给出当前 Python 的精确 `pip install` 命令；基本体/拆分/替换/剖面/测量不依赖它。
  - **退化几何诊断**：`mesh op=diagnose` 与 `validate(deep)` 新增绕向一致性、逐壳体积、退化三角形、重复/反向面、零厚度薄片、共面内部面、嵌套壳。`analyze` 在水密但绕向不一致时返回 `volumeMm3=null`（修复上一轮 1.7 / 15.9 cm³ 的假体积）。
  - **拓扑口径统一**：`mesh_quality` 先焊接顶点再统计边，`analyze`/`weld`/`cleanup`/`validate` 不再对同一网格给出矛盾结论。
  - **剖面与测量**：`mesh op=section`（2D 环/净面积/周长/可选 SVG）、`op=measure`（heightmap/spans/extent，可选 PNG）。
  - **渲染取景**：`render framing=scene|objects|bbox`、`zoom`、`camera{azimuth,elevation}`、`crop`；标签改为实际绘制/总对象数，修掉过滤后仍写全对象数的 bug。
  - **几何 diff**：`diff geometry=true` 按 0.001 mm 位置哈希比较三角面集合，给新增/删除/翻转面、体积/bbox 差值、5 mm 网格差异聚类。
  - **小噪音**：`inspect` 的 `parts` 改为"1 个部件（id=1, normal_part）"；`settings_limit` 对常用键生效；`mesh` 缺 object 给明确 `INVALID_ARGS`；`import_stl`/`build` 支持 `allow_overlap` 抑制预期重叠告警；`_diag` 不再丢弃结构化字段。
  - 自测新增 9 项（布尔/拆分/替换/基本体/剖面/测量/绕向/共面/几何 diff），契约样本新增 12 个（`selftest/fixtures_v05`）。

- **0.5.0 · 按需加载**
  - **八个工具改为门控注册**：新增 `lib/gate.mjs`，用 `register(definition, { when })` 在每步判定是否进入上下文。无关会话从固定约 6,900 token 降到 0；只提到过 3mf 时约 1,300 token；在项目目录里工作时不变。见上文"按需加载"。
  - **会话内粘住**：任一 `bambu3mf_*` 成功调用后，本会话内八个工具持续可见，避免换目录后工具突然消失。实现是在 `createTools` 返回处统一包一层 `execute`，不侵入各工具实现。
  - **`bambu3mf_slice` 额外要求 CLI**：`bambu-studio.exe` 解析不到时不占位（否则它只能报 `CLI_MISSING`）。探测结果按插件生命周期缓存，不进入每步路径。
  - **新增 `selftest/gating.mjs`**（36 项）：覆盖三层信号、粘住隔离、缓存过期，以及"条件永不抛异常"（10 种畸形 context + 5 种畸形 agent）与"缓存后单次成本 < 0.05ms"两条硬约束。
  - **修 `cwdOf` 的取值时机**：`ctx.get('workingDirectory')` 在 `apply()` 期间可能还没注入，捕获成 `undefined` 会让门控永久退回 `header.cwd`。改为在条件内部惰性读取。
  - 安装方式改为 `link:`（与 `dsh-mumu-debug` 一致），源码在 `H:\dsh_workspace\dsh-bambu-3mf`；原来的 `0.4.1` tarball 未改动，可随时回滚。

- **0.5.0 · 门控现场验证**

  用四个干净子会话做 A/B 对照（无 3mf 目录 / 87 个 3mf 目录 / 只有 STL / 只在消息里提到路径），
  再从 `session.v4.jsonl.zstd` 里读出每一步 `request/header` 实际收到的工具清单，
  发现门控在真实链路上有两处与设计不符，都已修复：

  - **缺陷 1：提到路径后工具会在同一轮里中途消失。** 原实现用"最近 20 个**事件**"当窗口，
    而一次工具调用大约追加 5 个事件（`tool/call`、`job/registered`、`job/settled`、`tool/result`、`step/end`），
    所以约 4 次调用后窗口就被挤出，提到过的 3mf 路径失效。实测同一会话内工具数 `0 → 3 → 0` 抖动。
    现在窗口按**用户消息条数**计，8 次工具调用后仍稳定。

  - **缺陷 2：一轮的第一步看不到当轮那条用户消息。** `agent-loop` 的 `preStep` 顺序是
    `inbox.claim()` → `tools.resolve()` → 构建请求时才 `append("user/message")`，
    所以 `when` 求值时"刚说'打开 xxx.3mf'"这句话**还不在 log 里**——恰恰是最该放行的那一步被漏掉。
    该消息此刻仍以 `agent/inbox/spliced` 的 `inserted` 载荷持久存在，现在两个来源都读，按 `id` 去重。

  - **同时收紧了信噪比**：`source.kind !== 'user'` 的消息（`runtime-context` 快照、`skill-catalog`）不再参与扫描。
    这类消息每轮都会重述整份环境，参与匹配会让"提到过 3mf"变成假阳性来源。

  - 回归测试 28 → 36 项，新增：`a mention survives eight tool calls (no mid-turn flicker)`、
    `runtime-context messages are not scanned for mentions`、
    `a message still in the inbox is scanned (first step of a turn)`、
    `a committed message read from both sources still admits the light tools`。

- **0.4.1**（重新出包）
  - `bambu3mf_slice` 的 output schema：`sliceStats.featureTimes` 由 `additionalProperties: { type: "number" }` 放宽为 `additionalProperties: true`，说明文字补上"每个值都是秒数"。键由 Bambu 按实际特征动态给出，键集不固定。
  - 0.4.0 的 `.tgz` / `.zip` 与源码同名但内容已旧（`lib/tools.mjs` 在出包后又改过），本次以 0.4.1 重新出包；`files` 清单里的 `selftest/*` 收窄成 `selftest/*.py` + `selftest/*.mjs`，不再把 `__pycache__` 打进包。
- **0.4.0**（补打印时长分析的 4 个能力缺口）
  - **新工具 `bambu3mf_diff`**：两份 3MF 逐键对比 project_settings（复用 `settings_keys` 的前缀/通配语义）、对象（名称/盘/挤出机/面数/包围盒/位置）、盘归属与切片结果。没有 `keys` 时只列差异；给了 `keys` 时连"相同"一起列出，回答"这个键到底一样吗"。数值字符串按数值比较，避免 `0.2` 与 `"0.2"` 这种假差异。
  - **`bambu3mf_inspect` 读切片报告**：从 `Metadata/slice_info.config` 取预估时长、耗材重量/米数、每盘对象与警告，`sliceInfo` 进结构化返回，摘要直接给一行"已切片：盘 1 · 预估 34.4 分钟 · 6.15 g"。解析失败只加一条诊断，不让 inspect 挂掉。
  - **`bambu3mf_slice` 给出耗时归因**：`sliceStats.featureTimes` 是 Bambu 算的分特征秒数（外墙/内墙/填充/空驶…），摘要多一行分解；`plateStats` 带每盘总时长、切片进程与导出耗时（新版在 `times` 里，毫秒换算）。`estimateMinutes` 改用 `total_predication`，与 Studio 显示的预估值一致（旧口径少算 1%~10%）。
  - **切片数据可信性**：`result.json` 与导出包都校验 mtime，上次的同名产物不再算本次成功；`result.json` 过期时忽略其时间并报 `STALE_RESULT`。
  - **`bambu3mf_mesh op=analyze`**：只读给出体积、表面积、水密/开放边、连通壳数、**通孔数（genus）**、**悬垂面积（扣掉贴床面）**、层数估算。实测 53.7 万面的网孔板：genus 1177、45° 悬垂 15.8% 中真正需支撑只有 0.3%、515 层，耗时约 5 s。
  - 契约测试新增 3 个样本（analyze / diff / 切片包 inspect），新增 `selftest/slice_result.mjs` 覆盖 JS 侧 result.json 解析；自测新增 4 项。
- **0.3.2**（修 0.3.1 遗留的 1 个 P0）
  - **P0 修复：`mesh` / `settings` 的 `in_place` 结果被自己的 output schema 拒收。** 内核的 `_finish()` 在任何
    `in_place` 写盘后都会返回 `backup`（`<名>.bak-<时间戳>.3mf`），但只有 `edit` 声明了它，`mesh` / `settings`
    是 `additionalProperties: false`，于是**文件已写入、备份已生成，工具却报
    `returned invalid output: "value.backup" is not a declared property`**。调用方以为失败 → 重试 → 拉伸/缩放叠加
    （实测一次"盘 1 三件加宽 150%"的活被中断在第 2 件，文件停在半成品状态）。现在 `backup` 放进所有写盘工具共享的
    output schema 基类，由 `_finish()` 的下游统一覆盖，将来新增写盘工具不会再漏。
  - **契约测试补上写盘分支**：`selftest/make_fixtures.py` 之前 11 个样本全是 `dry_run` 或只读命令，永远看不到
    `backup`——这正是它没报警的原因。新增 `edit in_place` / `edit output` / `mesh in_place` /
    `settings in_place` 四个真实写盘样本（各用独立副本），并对 in_place 样本断言 `backup` 存在且文件真实存在。
    `selftest/schema_contract.mjs` 额外输出"声明了但没有样本覆盖"的属性清单，避免再出现没人测的分支。
  - 自测：契约样本 11 → 15。
- **0.3.1**（修 0.3.0 引入的 3 个 P0 + 6 个 P1）
  - **P0 修复：预检不再误拒可切工程。** 0.3.0 用固定的 10 mm 间距当阻断条件，结果把 Bambu 自己能切的
    `监控支架.3mf`（5 mm 间距）、`Module+A.3mf`、`YIBOX` 全部拒掉，摘要里"Bambu 会拒绝这片"是错的。
    现在每个盘只给一行"最紧的一对相距多远"（`PLATE_CLEARANCE`，纯信息），`OBJECT_TIGHT` 与距离阈值一并删除；
    `preflight` 只有**真实表面接触**才判不通过，`preflight: "strict"` 才把包围盒重叠也算上。
  - **P0 修复：预检建议坐标改为 `move` 增量。** 之前输出的是**盘内局部**的包围盒中心，却被当成
    `set_position` 的世界坐标使用，盘 2（原点 307.2）上算出的 `x≈10` 会把对象甩到别的盘去。
    增量与坐标系无关，同时给出换算后的世界坐标；`from`/`to` 统一为世界坐标。
  - **P0 修复：`edit` 输出不再违反自己的 schema。** `objects[].size` 之前已返回但 schema 未声明
    （`additionalProperties: false`），导致每次成功编辑都以 `INVALID_TOOL_OUTPUT` 收场——文件已写入、
    调用方却以为失败，重试 `move` 会叠加位移。现在 schema 声明并强制 `size`，且新增
    `selftest/schema_contract.mjs`：用工具自己声明的 schema 校验真实内核输出，Python 自测看不到这一层。
  - **P1 修复**：空网格对象不再让 `_nearest_distance` 抛 `ValueError`（→ 曾在 `save()` 内报错，文件已写却失败）；
    `stretch` 的 band 落在物体外从"静默平移整件"改为报错并给出该轴实际范围；
    `_key` 不再把带数值的 message 算进诊断身份（此前一次 1 mm 移动就把旧问题当"新增警告"重报）；
    `validate()` 去掉逐对最近距离后从 ~1.9 s 降到 ~0.13 s（YIBOX，42 对象），噪声从 87 条降到每盘 1 条；
    自动摆放与间距报告共用同一个常量，工具自己排的版不会再被自己告警；建议算不出的对象列入 `infeasible`。
  - 顺带：`duplicate` 的返回里带上新建对象；`_diag` 保留冲突对象对与量化字段，调用方不必解析中文。
  - 自测 33 → 38 项，新增 plate 2 建议坐标、可切工程不被拦、空网格、band 越界、性能/噪声、
    以及 JS output-schema 契约校验。
- **0.3.0**
  - **可见性修复**：`inspect detail=full` 之前只把矩阵 / parts / 连通壳 / 包内条目 / `projectSettings` 放进结构化返回，JS 渲染层只输出 `summary`，于是这些字段对模型完全不可见。现在它们全部写进文本；新增 `settings_keys`（支持 `wipe_tower` 前缀与 `*speed*` 通配）与 `settings_limit`，不传时只列常用项，避免 500+ 键刷屏。
  - **`edit` 回读结果**：摘要里直接给出改动后每个对象的位置、尺寸、底面 z、盘归属，不再只有一行"3 处改动"。
  - **新增 `mesh op=stretch`**：按区间拉长/压缩，区间外刚性平移，拓扑与三角面数不变。这是"两端不变、只在中间加长"的正确做法（`scale` 会把壁厚和卡扣一起放大）。
  - **间距报告**：新增 `preflight` 命令，切片失败时附带本地测量结论，不再只有 CLI 那句通用报错。（0.3.0 的阈值式阻断判断有误，已在 0.3.1 改为建议式。）
  - **`select` 撕裂告警**：`selection.seamEdges` 报出选区边界横穿连续曲面时会裂开的边数（此前会静默产生开放边）。
  - **共享网格脱钩可见**：`detachedMesh` + 摘要说明"已为对象 N 单独复制一份，其余对象保持原样"，不再需要猜兄弟对象会不会一起变。
  - **`set_position` 加了 `z_mode`**：`bed` 表示"我要底面在 z 这个高度"，`origin`（默认）仍是矩阵偏移，摘要统一显示底面 z。
  - 自测从 28 项扩到 33 项。
- **0.2.0**
  - 新增 `bambu3mf_mesh` 的 `components` 操作与 `select` 参数（按连通体或包围盒选子网格），`pivot: "selection"`；`inspect detail=full` 会带 `components`。这类"只改一个零件"的活从 export→外部改→import→delete 的 6~8 步降到 1 步，且不再经历 STL 往返（不会丢/焊掉退化面）。
  - 修复 `delete` 残留 `<assemble_item>` 导致 Bambu 报 "The input model file to the slicer can not be parsed"。
  - 修复 `delete` 与几何重接线留下悬空 `<Relationship>`。
  - 新增 `ASSEMBLE_STALE_OBJECT` / `DANGLING_RELATIONSHIP` 诊断；修正 `ORPHAN_OBJECT_FILE` 对本次已删除条目的误报。
  - 自测从 22 项扩到 28 项。

## 安装 / 卸载

插件已经以 `link:` 方式装进当前 `desktop` profile：

```text
plugin_manager install_bundle  target="link:H:/dsh_workspace/dsh-bambu-3mf"
plugin_manager remove_bundle   target="@local/dsh-bambu-3mf"
```

改动 Python 内核即时生效（每次调用新起进程）；改动 `index.js` / `lib/*.mjs` 需要重新加载插件（重新安装或重启 Harness）。

## 搬到其他机器

插件是纯 JS + 纯 Python 源码，**没有 npm 依赖、没有编译步骤、清单里没有绝对路径**，所以搬机器 = 拷贝 + 装一次。要出制品时在包目录跑 `pnpm pack` 得到 tgz，或直接把源码目录打成 zip（仓库只放源码，下表是出包后的文件名）：

| 文件 | 结构 | 用途 |
|---|---|---|
| `dsh-bambu-3mf-0.5.0.zip` | 顶层一个 `dsh-bambu-3mf-0.5.0/` 目录 | 手工拷贝用，解压后这个目录就是包目录 |
| `local-dsh-bambu-3mf-0.5.0.tgz` | npm/pnpm 格式，`package/` 前缀 | 安装器可直接把这个文件路径当 install spec |

`node_modules`、`__pycache__`、`*.pyc` 都已被排除。

**目标机器前提**（缺一条相应工具就会报错，其余工具照常可用）：

1. 任意 DSH Desktop 版本（工具面需要 `tools` 服务与 `attachments`，即标准 desktop/web profile）。
2. 一个装了 **lxml、numpy、Pillow** 的 Python；DSH 自带运行时
   `%USERPROFILE%\.dsh\dsh-runtimes\<runtime>\dependencies\python\python.exe` 就满足，
   `pythonPath` 留 `null` 会自动挑最新的那个，然后才退回 PATH。
   **要用布尔建模**时再装一次 `manifold3d`（约 1 MB，Apache-2.0）：
   `"<该 python.exe>" -m pip install manifold3d==3.5.4`。不装也能用基本体/拆分/替换/剖面/测量。
3. 只有要切片时才需要 **Bambu Studio**；`bambuStudioPath` 留 `null` 会自动找
   `C:\Program Files\Bambu Studio\` 等常见位置。不装也能 inspect / diff / edit / mesh / build（除布尔）/ render / settings。

**装法 A：目标机器上有个 agent（推荐）**

```text
plugin_manager install_bundle  target="C:/Users/<you>/dsh-plugins/local-dsh-bambu-3mf-0.5.0.tgz"
# 或者指向解压后的目录：
plugin_manager install_bundle  target="C:/Users/<you>/dsh-plugins/dsh-bambu-3mf"
```

返回 `application: applied` 即已生效；八个 `bambu3mf_*` 工具随后出现在新会话里。

**装法 B：没有 agent，手工改 profile**

1. 解压到稳定目录，例如 `C:\Users\<you>\dsh-plugins\dsh-bambu-3mf`。
2. 在 `%USERPROFILE%\.dsh\profiles\<profile>\package.json` 的 `dependencies` 里加
   `"@local/dsh-bambu-3mf": "link:C:/Users/<you>/dsh-plugins/dsh-bambu-3mf"`
   （或 `"file:C:/Users/<you>/dsh-plugins/local-dsh-bambu-3mf-0.5.0.tgz"`），
   并把 `"@local/dsh-bambu-3mf"` 追加进 `dsh.profile.bundles`。
3. 在该 profile 目录跑 `pnpm install`（DSH 自带 pnpm 在
   `%USERPROFILE%\.dsh\dsh-runtimes\<runtime>\dependencies\pnpm\bin\pnpm.mjs`），然后重启 DSH。

**装法 C：命令行一行搞定**（等价于 A，走 `dsh plugin` 转发 pnpm 并自动补 `dsh.profile.bundles`）

```powershell
dsh plugin --profile desktop add "C:\Users\<you>\dsh-plugins\local-dsh-bambu-3mf-0.5.0.tgz"
```

**搬过去要自己补的**

- 目标机器的 Python 若缺 lxml/numpy/Pillow，先在 `cordis.patch.yml` 里把 `pythonPath` 指向一个带的解释器；
  布尔还要 `pip install manifold3d==3.5.4`。渲染宽度、采样上限、切片超时也在同一个文件里调。
- 3MF 样本文件不在这个包里（`selftest/run_selftest.py` 默认读 `D:\下载`，用 `--samples` 指到目标机的样本目录，
  加 `--skip-slice` 可跳过真实切片）。
