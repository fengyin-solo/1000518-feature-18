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
| 矿产评价 | `mineral` | 矿化线索 | 线索编号、勘探区、矿种、评价等级 |
| 环境地质 | `environmental` | 环境调查点 | 调查编号、调查区域、灾害类型 |

## 矿产评价：状态梯级板

矿化线索登记后线索编号即固定，状态只能沿梯级板逐格推进，不允许跳级或回退：

```text
待踏勘 ──安排踏勘──▶ 踏勘中 ──开始评价──▶ 评价中 ──提交结论──▶ 已评价
```

- **跳级拦截**：动作只能在指定前置状态执行（如「提交结论」仅在「评价中」可用），
  跳级/回退提交返回 `ok=false` 并说明当前状态、缺的梯级与正确路径。
- **修订版本**：重开已评价线索会先生成归档快照，再开一条 `revision+1` 的新版本
  从「待踏勘」重新流转；新版本禁止沿用任何已归档版本的旧结论。
- **归档只读**：归档后主表移除、快照进 `mineral_archive`，归档版本读不出也写不进新值。
- **三处同步**：提交结论在同一事务内同步矿产评价台账（`/api/mineral/ledger`）、
  偏离点图清单（`/api/mineral/deviations`）与验证待办（`/api/mineral/todos`）；
  归档时一并冻结台账、上图偏离点、关闭待办。
- **冲突以现场复核为准**：矿种与评价等级不配套时，结论提交被拦；完成「现场复核」后
  以最近一次复核确认的矿种/等级落账，并登记偏离点。历史评价按归档版本原样留档。
- **并发与幂等**：按线索编号加锁 + 版本校验，并发流转只有一个成功（落败方
  `conflict=true`）；携带相同 `request_id` 的重复提交回放首次结果、不重复生效；
  归档/撤销/重开全部走事务提交，失败整体回滚。
- **老线索迁移**：没有评价等级的历史线索按踏勘日期补档（一年内一类、两年内二类、
  更早三类），服务启动时自动迁移且幂等。
- 版本查阅：`GET /api/mineral/{线索编号}/versions`；归档版本读取：
  `GET /api/mineral/{线索编号}?revision=N`（返回只读快照）。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
