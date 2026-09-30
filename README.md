# 地质勘探数据管理平台

面向地质勘探的钻孔编录、岩心取样、物探数据、化探分析、测绘资料与储量估算的综合数据管理后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 钻孔编录 | `borehole` | 钻孔 | 钻孔编号、勘探区、孔口坐标 |
| 岩心管理 | `core` | 岩心样本 | 岩心编号、所属钻孔、取样深度起 |
| 地层划分 | `stratigraphy` | 地层单元 | 单元编号、钻孔编号、地层名称 |
| 地球物理 | `geophysics` | 物探测线 | 测线编号、勘探区、物探方法 |
| 化探分析 | `geochem` | 化探样品 | 样品编号、样品类型、采样点位 |
| 化验数据 | `assay` | 化验结果 | 化验编号、样品编号、元素名称 |
| 地质填图 | `mapping` | 填图单元 | 图幅编号、图幅名称、比例尺 |
| 测绘控制 | `survey_point` | 控制点 | 点号、点类型、坐标X |
| 钻探日志 | `drilling_log` | 钻探记录 | 日志编号、钻孔编号、钻进深度 |
| 储量估算 | `reserve` | 矿体块段 | 块段编号、矿体名称、面积 |
| 样品登记 | `sample_registry` | 送检样品 | 送检编号、样品名称、采样位置 |
| 勘探设备 | `equipment` | 勘探仪器 | 仪器编号、仪器名称、型号规格 |
| 水文地质 | `hydro` | 水文观测点 | 观测编号、观测类型、所在钻孔 |
| 剖面编录 | `section` | 实测剖面 | 剖面编号、剖面名称、剖面长度 |
| 地质报告 | `geological_report` | 勘探报告 | 报告编号、勘探区、报告类型 |
| 遥感解译 | `remote` | 遥感数据 | 数据编号、数据源、分辨率 |
| 矿产评价 | `mineral` | 矿化线索 | 线索编号、勘探区、矿种 |
| 环境地质 | `environmental` | 环境调查点 | 调查编号、调查区域、灾害类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 矿产评价 · 状态梯级板

矿产评价（`mineral`）的矿化线索按“状态梯级板”管理，业务规则集中在
`app/services/mineral.py`，老数据迁移在 `app/services/mineral_migration.py`。

- 线索编号确定后只能逐级流转：`待踏勘 → 踏勘中 → 评价中 → 已评价`。
  跳级提交、同级重复提交、从“已评价”撤销都会被拦下并在 `message` 说明原因。
- **重开**“已评价”线索会生成新的修订版本（线索编号不变、`修订版本 +1`），
  旧版本整体进 `mineral_history` 留档，新版本从待踏勘重走，不能回到旧结论。
- **提交结论**时把结论同步到三张表：矿产评价台账 `mineral_ledger`、
  偏离点图清单 `mineral_deviation`（三类或命中负向描述才入册）、验证待办 `mineral_todo`。
- **归档**把当前已评价版本连同三表快照冻结进 `mineral_archive`；归档后只读，
  任何流转/复核/重开/再次同步都被拦下，现行台账不再读出该线索的新值。
- 矿种、评价等级与最近一次**现场复核**（`mineral_review`）冲突时以复核为准；
  历史评价按归档/修订版本留档，不被覆盖。
- 所有写动作在 `Store.transaction()` 整库快照事务内提交，异常整体回滚；
  并发流转靠全局锁 + `expectedToken`（rev_token）乐观校验只放一个成功，
  动作请求带 `idempotencyKey` 时同键只生效一次。
- 老线索缺“评价等级”的，在服务初始化时按踏勘时间一次性迁移补齐（一类/二类/三类），
  已有等级不覆盖，迁移只执行一次。

矿产评价新增接口（前缀 `/api/mineral`）：

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /boards` | 四档在线数量 + 三表现行/待办数量 |
| `GET /ledger?all_versions=` | 矿产评价台账（默认仅现行） |
| `GET /deviations` | 偏离点图清单 |
| `GET /todos` | 验证待办 |
| `GET /reviews?entry_id=` | 现场复核记录 |
| `GET /archive` | 归档版本（只读快照） |
| `GET /history?线索编号=` | 重开后的历史修订版本 |
| `POST /{id}/actions` | 动作：安排踏勘/开始评价/提交结论/撤销/重开/归档/现场复核 |

动作为 `POST /{id}/actions`，请求体形如
`{ "values": {"action": "提交结论", "评价结论": "...", "评价等级": "一类"},
   "expectedToken": 3, "idempotencyKey": "..." }`。

领域规则测试（仅标准库）：`cd backend && python3 -m unittest tests.test_mineral_ladder`。

