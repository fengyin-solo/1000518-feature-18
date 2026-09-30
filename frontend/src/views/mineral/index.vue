<template>
  <section class="page" data-module="mineral">
    <header class="page-head">
      <div>
        <h2>矿产评价 · 状态梯级板</h2>
        <p class="page-desc">
          线索编号确定后只能 待踏勘 → 踏勘中 → 评价中 → 已评价 逐级流转；跳级、终态改写、归档后写入都会被拦下并说明原因。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记矿化线索</button>
        <button class="btn" type="button" @click="exportRows">导出矿产评价清单</button>
      </div>
    </header>

    <!-- 状态梯级板 -->
    <div class="ladder">
      <template v-for="(status, index) in statuses" :key="status">
        <article class="rung" :class="{ active: true }">
          <span class="rung-name">{{ status }}</span>
          <strong class="rung-value">{{ boards[status] ?? 0 }}</strong>
        </article>
        <span v-if="index < statuses.length - 1" class="rung-arrow">→</span>
      </template>
      <article class="rung archived">
        <span class="rung-name">已归档</span>
        <strong class="rung-value">{{ boards.archived ?? 0 }}</strong>
      </article>
    </div>

    <div class="stat-row">
      <article class="stat-card"><span class="stat-label">现行台账</span><strong class="stat-value">{{ boards.ledger_current ?? 0 }}</strong></article>
      <article class="stat-card"><span class="stat-label">现行偏离点</span><strong class="stat-value">{{ boards.deviation_current ?? 0 }}</strong></article>
      <article class="stat-card"><span class="stat-label">待验证待办</span><strong class="stat-value">{{ boards.todo_pending ?? 0 }}</strong></article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>线索编号</span>
        <input v-model="keyword" placeholder="按线索编号检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <label class="filter-item checkbox">
        <input v-model="includeArchived" type="checkbox" />
        <span>含已归档版本</span>
      </label>
      <button class="btn" type="submit">查询</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ frozen: row.已归档 }">
          <td v-for="column in columns" :key="column">
            <template v-if="column === 'status'">
              <span class="badge" :class="badgeClass(row.status)">{{ row[column] ?? '—' }}</span>
              <em v-if="row.已归档" class="frozen-tag">已冻结</em>
            </template>
            <template v-else>{{ row[column] || '—' }}</template>
          </td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              :disabled="working"
              @click="onAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无矿产评价数据，可先登记矿化线索</td>
        </tr>
      </tbody>
    </table>

    <!-- 台账 / 偏离点 / 待办 / 归档 -->
    <section class="sync-panels">
      <article class="panel">
        <h3>矿产评价台账（现行）</h3>
        <ul><li v-for="item in ledger" :key="String(item.id)">v{{ item.修订版本 }} · {{ item.线索编号 }} · {{ item.矿种 }} · {{ item.评价等级 }} · {{ item.评价结论 }}</li></ul>
        <p v-if="!ledger.length" class="empty-line">暂无现行台账</p>
      </article>
      <article class="panel">
        <h3>偏离点图清单</h3>
        <ul><li v-for="item in deviations" :key="String(item.id)">{{ item.线索编号 }} · {{ item.评价等级 }} · {{ item.偏离摘要 }}</li></ul>
        <p v-if="!deviations.length" class="empty-line">暂无现行偏离点</p>
      </article>
      <article class="panel">
        <h3>验证待办</h3>
        <ul><li v-for="item in todos" :key="String(item.id)">[{{ item.优先级 }}] {{ item.待办事项 }}（{{ item.待办状态 }}）</li></ul>
        <p v-if="!todos.length" class="empty-line">暂无待验证待办</p>
      </article>
      <article class="panel">
        <h3>归档版本（只读）</h3>
        <ul><li v-for="item in archive" :key="String(item.id)">{{ item.线索编号 }} v{{ item.修订版本 }} · {{ item.评价等级 }} · {{ item.归档时间 }}</li></ul>
        <p v-if="!archive.length" class="empty-line">暂无归档版本</p>
      </article>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条线索记录</span>
      <span v-if="message" :class="messageOk ? 'ok-text' : 'error-text'">{{ message }}</span>
    </footer>

    <!-- 登记 -->
    <div v-if="dialog === 'create'" class="modal-mask" @click.self="dialog = null">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记矿化线索（初始：待踏勘）</h3>
        <label v-for="field in createFields" :key="field.key">
          <span>{{ field.label }}</span>
          <input v-model="createForm[field.key]" :required="field.required" />
        </label>
        <div class="modal-actions">
          <button class="btn" type="button" @click="dialog = null">取消</button>
          <button class="btn primary" type="submit" :disabled="working">确定</button>
        </div>
      </form>
    </div>

    <!-- 提交结论 / 重开 / 现场复核 表单 -->
    <div v-if="dialog && dialog !== 'create'" class="modal-mask" @click.self="dialog = null">
      <form class="modal" @submit.prevent="submitDialog">
        <h3>{{ dialog }} · {{ target?.线索编号 }} v{{ target?.修订版本 }}</h3>

        <template v-if="dialog === '提交结论'">
          <label><span>评价结论 *</span><textarea v-model="form.评价结论" rows="3" required></textarea></label>
          <label><span>评价等级 *</span>
            <select v-model="form.评价等级" required>
              <option value="" disabled>请选择</option>
              <option v-for="grade in grades" :key="grade" :value="grade">{{ grade }}</option>
            </select>
          </label>
          <p class="hint">若与最近一次现场复核冲突，系统会以现场复核为准并在此说明。</p>
        </template>

        <template v-else-if="dialog === '重开'">
          <p class="hint">重开后旧版本结论留档，新版本从待踏勘重新逐级流转，不允许回到旧结论。</p>
          <label><span>重开原因 *</span><textarea v-model="form.重开原因" rows="3" required></textarea></label>
        </template>

        <template v-else-if="dialog === '现场复核'">
          <label><span>矿种</span><input v-model="form.矿种" :placeholder="String(target?.矿种 ?? '')" /></label>
          <label><span>评价等级</span>
            <select v-model="form.评价等级">
              <option value="">不改</option>
              <option v-for="grade in grades" :key="grade" :value="grade">{{ grade }}</option>
            </select>
          </label>
          <label><span>复核人</span><input v-model="form.复核人" /></label>
          <label><span>说明</span><input v-model="form.说明" /></label>
          <p class="hint">至少填写矿种或评价等级之一；冲突以最近一次现场复核为准。</p>
        </template>

        <div class="modal-actions">
          <button class="btn" type="button" @click="dialog = null">取消</button>
          <button class="btn primary" type="submit" :disabled="working">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Scalar = string | number | boolean | null
type Row = Record<string, Scalar>
type AnyRow = Record<string, unknown>
type BoardCounts = Record<string, number>

const ENDPOINT = '/api/mineral'
const columns = ['线索编号', '勘探区', '矿种', '矿化类型', '发现方式', '踏勘日期', '评价等级', '评价结论', 'status', '修订版本']
const statuses = ['待踏勘', '踏勘中', '评价中', '已评价'] as const
const grades = ['一类', '二类', '三类']

const rows = ref<Row[]>([])
const total = ref(0)
const working = ref(false)
const message = ref('')
const messageOk = ref(false)

const keyword = ref('')
const statusFilter = ref('')
const includeArchived = ref(false)

const boards = ref<BoardCounts>({})
const ledger = ref<Row[]>([])
const deviations = ref<Row[]>([])
const todos = ref<Row[]>([])
const archive = ref<Row[]>([])

const dialog = ref<null | 'create' | '提交结论' | '重开' | '现场复核'>(null)
const target = ref<Row | null>(null)
const form = reactive<Record<string, string>>({ 评价结论: '', 评价等级: '', 重开原因: '', 矿种: '', 复核人: '', 说明: '' })
const createFields = [
  { key: '线索编号', label: '线索编号 *', required: true },
  { key: '勘探区', label: '勘探区 *', required: true },
  { key: '矿种', label: '矿种 *', required: true },
  { key: '矿化类型', label: '矿化类型', required: false },
  { key: '发现方式', label: '发现方式', required: false },
  { key: '踏勘日期', label: '踏勘日期', required: false },
  { key: '评价等级', label: '评价等级', required: false },
] as const
const createForm = reactive<Record<string, string>>({})

function notify(text: string, ok: boolean) {
  message.value = text
  messageOk.value = ok
}

function availableActions(row: Row): string[] {
  if (row.已归档) return []
  switch (row.status) {
    case '待踏勘': return ['安排踏勘']
    case '踏勘中': return ['开始评价', '撤销']
    case '评价中': return ['提交结论', '撤销']
    case '已评价': return ['现场复核', '重开', '归档']
    default: return []
  }
}

function badgeClass(status: Scalar): string {
  return { 待踏勘: 'b-gray', 踏勘中: 'b-blue', 评价中: 'b-amber', 已评价: 'b-green' }[String(status)] ?? 'b-gray'
}

function openCreate() {
  for (const field of createFields) createForm[field.key] = ''
  dialog.value = 'create'
}

function onAction(action: string, row: Row) {
  if (action === '提交结论' || action === '重开' || action === '现场复核') {
    target.value = row
    Object.keys(form).forEach((key) => { form[key] = '' })
    if (action === '现场复核') form.矿种 = String(row.矿种 ?? '')
    dialog.value = action
    return
  }
  // 安排踏勘 / 开始评价 / 撤销 / 归档：直接提交，携带当前版本号做并发校验
  void postAction(action, row, {})
}

async function submitDialog() {
  if (!target.value || !dialog.value) return
  const payload: Record<string, string> = { action: dialog.value }
  if (dialog.value === '提交结论') {
    payload.评价结论 = form.评价结论
    payload.评价等级 = form.评价等级
  } else if (dialog.value === '重开') {
    payload.重开原因 = form.重开原因
  } else if (dialog.value === '现场复核') {
    if (form.矿种 && form.矿种 !== String(target.value.矿种 ?? '')) payload.矿种 = form.矿种
    if (form.评价等级) payload.评价等级 = form.评价等级
    if (form.复核人) payload.复核人 = form.复核人
    if (form.说明) payload.说明 = form.说明
  }
  await postAction(dialog.value, target.value, payload)
  if (messageOk.value) dialog.value = null
}

async function submitCreate() {
  const values: Record<string, string> = {}
  for (const field of createFields) {
    if (createForm[field.key]) values[field.key] = createForm[field.key]
  }
  await callApi(ENDPOINT, { method: 'POST', body: JSON.stringify({ values }) })
  if (messageOk.value) dialog.value = null
  await reload()
}

async function postAction(action: string, row: Row, values: Record<string, string>) {
  // 用“线索id+动作+版本号”做幂等键：同一动作对同一版本重复点击只生效一次。
  const idempotencyKey = `${row.id}:${action}:v${row.修订版本}:t${row.rev_token}`
  await callApi(`${ENDPOINT}/${row.id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ values: { action, ...values }, expectedToken: Number(row.rev_token ?? 0), idempotencyKey }),
  })
  await reload()
}

async function callApi(path: string, init?: RequestInit) {
  if (working.value) return
  working.value = true
  message.value = ''
  try {
    const response = await request(path, init)
    const payload = (await response.json().catch(() => ({}))) as { ok?: boolean; message?: string }
    if (!response.ok || payload.ok === false) {
      notify(payload.message || `操作未生效（HTTP ${response.status}）`, false)
    } else {
      notify(payload.message || '操作已生效', true)
    }
  } catch (error) {
    notify(error instanceof Error ? error.message : '接口请求失败', false)
  } finally {
    working.value = false
  }
}

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const response = await request(path)
    if (!response.ok) return null
    return (await response.json()) as T
  } catch {
    return null
  }
}

async function reloadPanels() {
  const [b, l, d, t, a] = await Promise.all([
    getJson<BoardCounts>(`${ENDPOINT}/boards`),
    getJson<{ items: Row[] }>(`${ENDPOINT}/ledger`),
    getJson<{ items: Row[] }>(`${ENDPOINT}/deviations`),
    getJson<{ items: Row[] }>(`${ENDPOINT}/todos?status=待验证`),
    getJson<{ items: Row[] }>(`${ENDPOINT}/archive`),
  ])
  if (b) boards.value = b
  ledger.value = l?.items ?? []
  deviations.value = d?.items ?? []
  todos.value = t?.items ?? []
  archive.value = a?.items ?? []
}

async function reload() {
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  if (includeArchived.value) query.set('include_archived', 'true')
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    const payload = (await response.json()) as { items?: Row[]; total?: number }
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    notify(error instanceof Error ? error.message : '矿化线索列表读取失败', false)
  }
  await reloadPanels()
}

function exportRows() {
  const suffix = includeArchived.value ? '?include_archived=true' : ''
  window.open(`${ENDPOINT}/export${suffix}`, '_blank')
}

onMounted(reload)
</script>

<style scoped>
.ladder { display: flex; align-items: stretch; gap: 8px; margin-bottom: 12px; }
.rung { flex: 1; background: #fff; border: 1px solid var(--border); border-left: 4px solid var(--brand); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 4px; }
.rung.archived { border-left-color: #98a2b3; }
.rung-name { color: var(--muted); font-size: 12px; }
.rung-value { font-size: 22px; }
.rung-arrow { align-self: center; color: var(--muted); }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; font-style: normal; }
.b-gray { background: #f2f4f7; color: #475467; }
.b-blue { background: #e0f2fe; color: #0369a1; }
.b-amber { background: #fef3c7; color: #b45309; }
.b-green { background: #dcfae6; color: #067647; }
.frozen { color: var(--muted); }
.frozen-tag { margin-left: 6px; color: #98a2b3; font-size: 12px; font-style: normal; }
.sync-panels { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.panel h3 { margin: 0 0 8px; font-size: 14px; }
.panel ul { margin: 0; padding-left: 18px; font-size: 13px; display: grid; gap: 4px; }
.empty-line { color: var(--muted); font-size: 12px; margin: 0; }
.ok-text { color: #067647; }
.checkbox { flex-direction: row; align-items: center; gap: 6px; }
.modal-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45); display: flex; align-items: center; justify-content: center; z-index: 20; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; width: 420px; display: grid; gap: 10px; }
.modal h3 { margin: 0 0 4px; }
.modal label { display: grid; gap: 4px; font-size: 12px; color: var(--muted); }
.modal input, .modal textarea, .modal select { width: 100%; box-sizing: border-box; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 6px; }
.hint { font-size: 12px; color: var(--muted); margin: 0; }
</style>
