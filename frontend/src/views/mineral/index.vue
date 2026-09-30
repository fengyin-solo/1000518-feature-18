<template>
  <section class="page" data-module="mineral">
    <header class="page-head">
      <div>
        <h2>矿产评价管理 · 状态梯级板</h2>
        <p class="page-desc">
          线索编号确定后只能沿 待踏勘 → 踏勘中 → 评价中 → 已评价 逐格推进；
          跳级提交会被拦下。重开已评价线索生成新修订版本，旧结论按归档版本留档。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记矿化线索</button>
        <button class="btn" type="button" @click="exportRows">导出矿产评价清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="tabs">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        :class="['tab', { active: activeTab === tab.key }]"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <template v-if="activeTab === 'clues'">
      <form class="filter-bar" @submit.prevent="reload">
        <label class="filter-item">
          <span>线索编号</span>
          <input v-model="keyword" placeholder="按线索编号检索" />
        </label>
        <label class="filter-item">
          <span>线索状态</span>
          <select v-model="statusFilter">
            <option value="">全部状态</option>
            <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <label class="filter-item checkbox">
          <input v-model="includeArchived" type="checkbox" @change="reload" />
          <span>含归档版本</span>
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>线索编号</th>
            <th>修订</th>
            <th>勘探区</th>
            <th>矿种</th>
            <th>踏勘日期</th>
            <th>评价等级</th>
            <th>评价结论</th>
            <th style="min-width: 260px">状态梯级板</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="`${row['线索编号']}@${row.revision ?? 1}`">
            <td>
              <button class="link" type="button" @click="openVersions(row)">{{ row['线索编号'] }}</button>
            </td>
            <td>v{{ row.revision ?? 1 }}<span v-if="row['已归档']" class="tag archived">已归档</span></td>
            <td>{{ row['勘探区'] || '—' }}</td>
            <td>{{ row['矿种'] || '—' }}</td>
            <td>{{ row['踏勘日期'] || '—' }}</td>
            <td>
              <span v-if="row['评价等级']" :class="['grade', gradeClass(row['评价等级'])]">{{ row['评价等级'] }}</span>
              <span v-else class="muted">待补档</span>
            </td>
            <td>{{ row['评价结论'] || '—' }}</td>
            <td>
              <ol class="ladder">
                <li
                  v-for="(step, idx) in ladderSteps"
                  :key="step"
                  :class="ladderStepClass(row, idx)"
                >
                  {{ step }}
                </li>
              </ol>
            </td>
            <td class="row-actions wrap">
              <button
                v-for="action in availableActions(row)"
                :key="action.name"
                class="link"
                type="button"
                @click="openAction(action.name, row)"
              >
                {{ action.label }}
              </button>
              <span v-if="!availableActions(row).length" class="muted">无可用动作</span>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="9" class="empty-state">暂无矿产评价数据，可先登记矿化线索</td>
          </tr>
        </tbody>
      </table>
    </template>

    <template v-else-if="activeTab === 'ledger'">
      <table class="data-table">
        <thead>
          <tr><th>线索编号</th><th>修订</th><th>勘探区</th><th>矿种</th><th>评价等级</th><th>评价结论</th><th>最近复核</th><th>冲突说明</th><th>台账状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in ledgerRows" :key="`${row['线索编号']}@${row.revision}`">
            <td>{{ row['线索编号'] }}</td>
            <td>v{{ row.revision }}</td>
            <td>{{ row['勘探区'] }}</td>
            <td>{{ row['矿种'] }}</td>
            <td><span :class="['grade', gradeClass(row['评价等级'])]">{{ row['评价等级'] }}</span></td>
            <td>{{ row['评价结论'] }}</td>
            <td>{{ row['最近复核'] || '—' }}</td>
            <td>{{ row['冲突说明'] || '—' }}</td>
            <td>
              <span v-if="row['已归档']" class="tag archived">已冻结</span>
              <span v-else-if="row['当前版本']" class="tag current">当前版本</span>
              <span v-else class="muted">历史</span>
            </td>
          </tr>
          <tr v-if="!ledgerRows.length"><td colspan="9" class="empty-state">台账暂无记录，提交评价结论后自动入账</td></tr>
        </tbody>
      </table>
    </template>

    <template v-else-if="activeTab === 'deviations'">
      <table class="data-table">
        <thead>
          <tr><th>线索编号</th><th>修订</th><th>勘探区</th><th>偏离原因</th><th>评价结论</th><th>登记日期</th><th>上图状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in deviationRows" :key="String(row.id)">
            <td>{{ row['线索编号'] }}</td>
            <td>v{{ row.revision }}</td>
            <td>{{ row['勘探区'] }}</td>
            <td>{{ row['偏离原因'] }}</td>
            <td>{{ row['评价结论'] }}</td>
            <td>{{ row['登记日期'] }}</td>
            <td>
              <span :class="['tag', row['状态'] === '已上图' ? 'done' : 'pending']">{{ row['状态'] }}</span>
            </td>
          </tr>
          <tr v-if="!deviationRows.length"><td colspan="7" class="empty-state">暂无偏离点；矿种与评价等级冲突时随结论自动登记</td></tr>
        </tbody>
      </table>
    </template>

    <template v-else-if="activeTab === 'todos'">
      <table class="data-table">
        <thead>
          <tr><th>线索编号</th><th>修订</th><th>验证事项</th><th>说明</th><th>登记日期</th><th>状态</th><th>关闭原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in todoRows" :key="String(row.id)">
            <td>{{ row['线索编号'] }}</td>
            <td>v{{ row.revision }}</td>
            <td>{{ row['事项'] }}</td>
            <td>{{ row['说明'] }}</td>
            <td>{{ row['登记日期'] }}</td>
            <td><span :class="['tag', row['状态'] === '待验证' ? 'pending' : 'done']">{{ row['状态'] }}</span></td>
            <td>{{ row['关闭原因'] || '—' }}</td>
          </tr>
          <tr v-if="!todoRows.length"><td colspan="7" class="empty-state">暂无验证待办</td></tr>
        </tbody>
      </table>
    </template>

    <template v-else>
      <header class="list-head">
        <h3>{{ versionCode }} 的修订版本</h3>
        <button class="btn ghost" type="button" @click="activeTab = 'clues'">返回线索列表</button>
      </header>
      <table class="data-table">
        <thead>
          <tr><th>修订版本</th><th>状态</th><th>矿种</th><th>评价等级</th><th>评价结论</th><th>归档时间</th><th>版本说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="ver in versionRows" :key="String(ver.revision)">
            <td>v{{ ver.revision }}</td>
            <td>{{ ver.status }}</td>
            <td>{{ ver['矿种'] }}</td>
            <td>{{ ver['评价等级'] || '—' }}</td>
            <td>{{ ver['评价结论'] || '—' }}</td>
            <td>{{ ver['归档时间'] || '—' }}</td>
            <td>
              <span v-if="ver['已归档']" class="tag archived">归档留档（只读）</span>
              <span v-else-if="ver['重开自版本']" class="tag current">重开自 v{{ ver['重开自版本'] }}</span>
              <span v-else class="tag active">活动版本</span>
            </td>
          </tr>
        </tbody>
      </table>
    </template>

    <footer class="page-foot">
      <span v-if="activeTab === 'clues'">共 {{ total }} 条矿产评价记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-if="successMessage" class="success-text">{{ successMessage }}</span>
    </footer>

    <!-- 登记 / 动作共用弹窗 -->
    <div v-if="dialog.open" class="modal-mask" @click.self="closeDialog">
      <form class="modal" @submit.prevent="submitDialog">
        <h3>{{ dialog.title }}</h3>
        <p v-if="dialog.hint" class="modal-hint">{{ dialog.hint }}</p>

        <template v-if="dialog.kind === 'create'">
          <label v-for="field in createFields" :key="field" class="form-item">
            <span>{{ field }}<em v-if="requiredFields.includes(field)">*</em></span>
            <input v-model="dialog.form[field]" :placeholder="`请输入${field}`" />
          </label>
        </template>

        <template v-else-if="dialog.kind === '安排踏勘'">
          <label class="form-item"><span>踏勘日期<em>*</em></span>
            <input v-model="dialog.form['踏勘日期']" type="date" />
          </label>
          <label class="form-item"><span>踏勘人员</span>
            <input v-model="dialog.form['踏勘人员']" placeholder="本次踏勘人员" />
          </label>
        </template>

        <template v-else-if="dialog.kind === '提交结论'">
          <label class="form-item"><span>评价等级<em>*</em></span>
            <select v-model="dialog.form['评价等级']">
              <option value="" disabled>请选择评价等级</option>
              <option value="一类">一类</option>
              <option value="二类">二类</option>
              <option value="三类">三类</option>
            </select>
          </label>
          <label class="form-item"><span>评价结论<em>*</em></span>
            <textarea v-model="dialog.form['评价结论']" rows="3" placeholder="请填写评价结论（修订版本不得沿用旧结论）"></textarea>
          </label>
        </template>

        <template v-else-if="dialog.kind === '现场复核'">
          <label class="form-item"><span>复核日期<em>*</em></span>
            <input v-model="dialog.form['复核日期']" type="date" />
          </label>
          <label class="form-item"><span>复核人员</span>
            <input v-model="dialog.form['复核人员']" />
          </label>
          <label class="form-item"><span>复核确认矿种</span>
            <input v-model="dialog.form['矿种']" placeholder="留空表示矿种不变" />
          </label>
          <label class="form-item"><span>复核确认评价等级</span>
            <select v-model="dialog.form['评价等级']">
              <option value="">留空表示等级不变</option>
              <option value="一类">一类</option>
              <option value="二类">二类</option>
              <option value="三类">三类</option>
            </select>
          </label>
          <label class="form-item"><span>复核意见</span>
            <textarea v-model="dialog.form['复核意见']" rows="2"></textarea>
          </label>
        </template>

        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="closeDialog">取消</button>
          <button class="btn primary" type="submit">确认提交</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/mineral'
const ladderSteps = ['待踏勘', '踏勘中', '评价中', '已评价']
const statuses = ['待踏勘', '踏勘中', '评价中', '已评价', '已撤销']
const createFields = ['线索编号', '勘探区', '矿种', '矿化类型', '发现方式', '踏勘日期']
const requiredFields = ['线索编号', '勘探区', '矿种']

const tabs = [
  { key: 'clues', label: '矿化线索' },
  { key: 'ledger', label: '矿产评价台账' },
  { key: 'deviations', label: '偏离点图清单' },
  { key: 'todos', label: '验证待办' },
  { key: 'versions', label: '修订版本' },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('clues')
const rows = ref<Row[]>([])
const ledgerRows = ref<Row[]>([])
const deviationRows = ref<Row[]>([])
const todoRows = ref<Row[]>([])
const versionRows = ref<Row[]>([])
const versionCode = ref('')
const total = ref(0)
const errorMessage = ref('')
const successMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const includeArchived = ref(false)

const stats = ref([
  { label: '待踏勘线索', value: 0 },
  { label: '踏勘中线索', value: 0 },
  { label: '评价中线索', value: 0 },
  { label: '已评价线索', value: 0 },
  { label: '已归档版本', value: 0 },
])

const dialog = reactive<{
  open: boolean
  kind: string
  title: string
  hint: string
  target: Row | null
  form: Record<string, string>
}>({ open: false, kind: '', title: '', hint: '', target: null, form: {} })

function flash(message: string, ok: boolean) {
  if (ok) {
    successMessage.value = message
    errorMessage.value = ''
  } else {
    errorMessage.value = message
    successMessage.value = ''
  }
  window.setTimeout(() => {
    successMessage.value = ''
    errorMessage.value = ''
  }, 6000)
}

function availableActions(row: Row): { name: string; label: string }[] {
  if (row['已归档']) {
    return [{ name: '重开', label: '重开新修订' }]
  }
  switch (row.status) {
    case '待踏勘':
      return [
        { name: '安排踏勘', label: '安排踏勘 →' },
        { name: '撤销', label: '撤销' },
      ]
    case '踏勘中':
      return [
        { name: '现场复核', label: '现场复核' },
        { name: '开始评价', label: '开始评价 →' },
        { name: '撤销', label: '撤销' },
      ]
    case '评价中':
      return [
        { name: '现场复核', label: '现场复核' },
        { name: '提交结论', label: '提交结论 →' },
        { name: '撤销', label: '撤销' },
      ]
    case '已评价':
      return [
        { name: '归档', label: '归档' },
        { name: '重开', label: '重开新修订' },
      ]
    default:
      return []
  }
}

function ladderStepClass(row: Row, idx: number) {
  const current = ladderSteps.indexOf(String(row.status))
  if (row.status === '已撤销') return idx === 0 ? 'cancel' : ''
  if (current < 0) return ''
  if (idx < current) return 'done'
  if (idx === current) return 'current'
  return ''
}

function gradeClass(grade: string | number | boolean | null | undefined) {
  return { 一类: 'g1', 二类: 'g2', 三类: 'g3' }[String(grade)] ?? ''
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  includeArchived.value = false
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function switchTab(key: typeof activeTab.value) {
  activeTab.value = key
  if (key === 'clues') void reload()
  if (key === 'ledger') void loadSide('ledger')
  if (key === 'deviations') void loadSide('deviations')
  if (key === 'todos') void loadSide('todos')
}

async function loadSide(kind: 'ledger' | 'deviations' | 'todos') {
  const response = await request(`${ENDPOINT}/${kind}`)
  if (!response.ok) {
    flash('配套清单读取失败', false)
    return
  }
  const payload = await response.json()
  if (kind === 'ledger') ledgerRows.value = payload.items ?? []
  if (kind === 'deviations') deviationRows.value = payload.items ?? []
  if (kind === 'todos') todoRows.value = payload.items ?? []
}

async function openVersions(row: Row) {
  const code = String(row['线索编号'])
  const response = await request(`${ENDPOINT}/${encodeURIComponent(code)}/versions`)
  if (!response.ok) {
    flash('修订版本读取失败', false)
    return
  }
  const payload = await response.json()
  versionCode.value = code
  versionRows.value = payload.items ?? []
  activeTab.value = 'versions'
}

function openCreate() {
  dialog.open = true
  dialog.kind = 'create'
  dialog.title = '登记矿化线索'
  dialog.hint = '线索编号一经确定即为流转主键，后续只能沿状态梯级板逐格推进。'
  dialog.target = null
  dialog.form = {}
}

const ACTION_META: Record<string, { title: string; hint: string }> = {
  安排踏勘: { title: '安排踏勘：待踏勘 → 踏勘中', hint: '只能从「待踏勘」推进，踏勘日期用于补档评价等级。' },
  开始评价: { title: '开始评价：踏勘中 → 评价中', hint: '' },
  提交结论: { title: '提交结论：评价中 → 已评价', hint: '结论同步到台账、偏离点图清单与验证待办；矿种与等级冲突时以最近一次现场复核为准。' },
  现场复核: { title: '登记现场复核', hint: '复核确认的矿种/评价等级将成为冲突判定的最近一次依据。' },
  归档: { title: '归档评价结论', hint: '归档后版本只读，任何动作都改不出新值。' },
  撤销: { title: '撤销矿化线索', hint: '撤销为终态，流转终止；与其它清单更新在同一事务提交。' },
  重开: { title: '重开新的修订版本', hint: '旧版结论先归档留档，新版本从「待踏勘」重新流转，且不得沿用旧结论。' },
}

function openAction(name: string, row: Row) {
  const meta = ACTION_META[name] ?? { title: name, hint: '' }
  dialog.open = true
  dialog.kind = name
  dialog.title = `${row['线索编号']} v${row.revision ?? 1} · ${meta.title}`
  dialog.hint = meta.hint
  dialog.target = row
  dialog.form = { revision: String(row.revision ?? 1) }
}

function closeDialog() {
  dialog.open = false
  dialog.target = null
  dialog.form = {}
}

async function submitDialog() {
  if (dialog.kind === 'create') {
    const missing = requiredFields.filter((field) => !dialog.form[field]?.trim())
    if (missing.length) {
      flash(`缺少必填字段：${missing.join('、')}`, false)
      return
    }
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { ...dialog.form } }),
    })
    const payload = await response.json()
    flash(payload.message, Boolean(payload.ok))
    if (payload.ok) {
      closeDialog()
      await reload()
    }
    return
  }

  const row = dialog.target
  if (!row) return
  const values: Record<string, string> = { action: dialog.kind, ...dialog.form }
  // 简单动作（开始评价/归档/撤销/重开）无需额外字段，后端直转。
  const response = await request(`${ENDPOINT}/${row['线索编号']}/actions`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  const payload = await response.json()
  if (!response.ok) {
    flash('矿产评价动作未生效，请稍后重试', false)
    return
  }
  flash(payload.message, Boolean(payload.ok))
  if (payload.ok) {
    closeDialog()
    if (activeTab.value === 'clues') await reload()
    else await loadSide(activeTab.value === 'versions' ? 'ledger' : activeTab.value as 'ledger')
  }
}

function computeStats(entries: Row[], archivedCount: number) {
  stats.value = [
    { label: '待踏勘线索', value: entries.filter((r) => r.status === '待踏勘').length },
    { label: '踏勘中线索', value: entries.filter((r) => r.status === '踏勘中').length },
    { label: '评价中线索', value: entries.filter((r) => r.status === '评价中').length },
    { label: '已评价线索', value: entries.filter((r) => r.status === '已评价' && !r['已归档']).length },
    { label: '已归档版本', value: archivedCount },
  ]
}

async function reload() {
  const params = new URLSearchParams()
  if (keyword.value.trim()) params.set('keyword', keyword.value.trim())
  if (statusFilter.value) params.set('status', statusFilter.value)
  params.set('include_archived', String(includeArchived.value))
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) throw new Error('矿化线索列表读取失败')
    const payload = await response.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    flash(error instanceof Error ? error.message : '矿产评价列表读取失败', false)
  }

  // 统计卡片始终基于「含归档」的全量口径，避免筛选后卡片数字跳动。
  const all = await request(`${ENDPOINT}?size=200&include_archived=true`)
  if (all.ok) {
    const payload = await all.json()
    const entries = (payload.items ?? []) as Row[]
    computeStats(
      entries.filter((r) => !r['已归档']),
      entries.filter((r) => r['已归档']).length,
    )
  }
}

onMounted(reload)
</script>

<style scoped>
.tabs { display: flex; gap: 4px; margin-bottom: 12px; border-bottom: 1px solid var(--border); }
.tab { border: none; background: none; padding: 8px 14px; cursor: pointer; font-size: 13px; color: var(--muted); border-bottom: 2px solid transparent; }
.tab.active { color: var(--brand); border-bottom-color: var(--brand); font-weight: 600; }
.muted { color: var(--muted); }
.success-text { color: #067647; }
.wrap { flex-wrap: wrap; }

.ladder { list-style: none; display: flex; align-items: center; gap: 4px; margin: 0; padding: 0; }
.ladder li { font-size: 12px; padding: 3px 8px; border-radius: 10px; background: #f2f4f7; color: var(--muted); white-space: nowrap; position: relative; }
.ladder li.done { background: #e7f6ee; color: #067647; }
.ladder li.current { background: var(--brand); color: #fff; font-weight: 600; }
.ladder li.cancel { background: #fef3f2; color: #b42318; }

.tag { display: inline-block; font-size: 11px; border-radius: 4px; padding: 1px 6px; margin-left: 4px; }
.tag.archived { background: #f2f4f7; color: #475467; }
.tag.current, .tag.active { background: #e0edff; color: #1f6feb; }
.tag.pending { background: #fffaeb; color: #b54708; }
.tag.done { background: #e7f6ee; color: #067647; }

.grade { display: inline-block; font-size: 12px; border-radius: 4px; padding: 1px 8px; }
.grade.g1 { background: #e7f6ee; color: #067647; }
.grade.g2 { background: #e0edff; color: #1f6feb; }
.grade.g3 { background: #f2f4f7; color: #475467; }

.modal-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45); display: flex; align-items: center; justify-content: center; z-index: 20; }
.modal { background: #fff; border-radius: 10px; padding: 20px 24px; width: 460px; max-width: 92vw; max-height: 88vh; overflow: auto; }
.modal h3 { margin: 0 0 8px; font-size: 16px; }
.modal-hint { font-size: 12px; color: var(--muted); margin: 0 0 12px; }
.form-item { display: block; margin-bottom: 10px; }
.form-item span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.form-item em { color: #b42318; font-style: normal; margin-left: 2px; }
.form-item input, .form-item select, .form-item textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; font-size: 13px; font-family: inherit; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.checkbox { display: flex; align-items: center; gap: 6px; }
.checkbox input { width: auto; }
.list-head { display: flex; justify-content: space-between; align-items: center; }
</style>
