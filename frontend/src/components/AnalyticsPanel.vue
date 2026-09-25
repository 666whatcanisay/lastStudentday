<template>
  <!--
    统计页签（方案 B §3.4 唯一口径）

    几条写死在界面上的规矩：
      1. 金额一律显示后端给的十进制字符串，**前端不做任何二次汇总** ——
         跨币种混算就是从"前端顺手加一遍"长出来的。
      2. 图表点击 → 带**相同过滤条件**打开明细；点金额图额外要求金额与币种都有效，
         抽屉里显示的条数与合计必须与图上数字一致（同源、同口径）。
      3. 没有数据就是空态，不画演示假图。
      4. 交易对象排行一次只选一个角色（发票买方/卖方、合同甲方/乙方），
         界面上明确写出"仅发票/仅合同"，避免被误读成双方合计。
      5. 图表用本地打包的 ECharts，构建产物里没有任何 CDN/外部资源引用。
  -->
  <div class="analytics-panel">
    <!-- ===== 过滤条件（图表与明细共用） ===== -->
    <el-card shadow="never" class="filter-card">
      <template #header>
        <div class="card-header">
          <el-icon><DataAnalysis /></el-icon>
          <span>统计筛选</span>
          <span class="card-subtitle">
            统计口径：未在回收站 + 有当前有效版本 + 非模拟引擎；金额只统计发票「价税合计」与合同「合同金额」
          </span>
        </div>
      </template>
      <el-form :inline="true" class="filter-form">
        <el-form-item label="类型">
          <el-select v-model="filters.docType" clearable placeholder="全部" style="width: 120px">
            <el-option label="发票" value="invoice" />
            <el-option label="合同" value="contract" />
            <el-option label="其他" value="other" />
          </el-select>
        </el-form-item>
        <el-form-item label="业务日期">
          <el-date-picker
            v-model="dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            style="width: 240px"
            @change="load"
          />
        </el-form-item>
        <el-form-item label="日期未知">
          <el-radio-group v-model="filters.dateUnknownMode" @change="load">
            <el-radio-button value="all">全部</el-radio-button>
            <el-radio-button value="only">仅未知</el-radio-button>
            <el-radio-button value="exclude">排除</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="币种">
          <el-select v-model="filters.currency" clearable placeholder="全部" style="width: 120px">
            <el-option v-for="c in currencyOptions" :key="c.value" :label="c.label" :value="c.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="校对">
          <el-select v-model="filters.reviewed" clearable placeholder="全部" style="width: 110px">
            <el-option label="已校对" value="true" />
            <el-option label="未校对" value="false" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :icon="Search" @click="load">统计</el-button>
          <el-button :icon="RefreshLeft" @click="resetFilters">重置</el-button>
        </el-form-item>
      </el-form>
      <el-alert
        v-if="dateRange && dateRange.length === 2 && filters.dateUnknownMode !== 'only'"
        type="info"
        :closable="false"
        show-icon
        class="filter-note"
      >
        <template #title>已按业务日期范围统计，日期未知的文档未纳入（不是用上传时间替代）</template>
      </el-alert>
    </el-card>

    <!-- ===== 数量卡 ===== -->
    <el-row :gutter="12" class="stat-row">
      <el-col :span="4">
        <el-card shadow="hover" class="mini-card">
          <div class="mini-label">有效文档</div>
          <div class="mini-value">{{ data.summary?.doc_count ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card shadow="hover" class="mini-card">
          <div class="mini-label">未校对</div>
          <div class="mini-value warn">{{ data.summary?.unreviewed_count ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card shadow="hover" class="mini-card">
          <div class="mini-label">
            日期缺失
            <el-tooltip :content="`分母：当前筛选出的 ${data.summary?.doc_count ?? 0} 份`" placement="top">
              <el-icon class="tip"><QuestionFilled /></el-icon>
            </el-tooltip>
          </div>
          <div class="mini-value warn">{{ data.summary?.missing_date_count ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card shadow="hover" class="mini-card">
          <div class="mini-label">
            金额缺失
            <el-tooltip :content="`分母：有金额字段的 ${data.summary?.missing_amount_denominator ?? 0} 份（不含「其他」类型）`" placement="top">
              <el-icon class="tip"><QuestionFilled /></el-icon>
            </el-tooltip>
          </div>
          <div class="mini-value warn">{{ data.summary?.missing_amount_count ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card shadow="hover" class="mini-card">
          <div class="mini-label">
            币种缺失
            <el-tooltip :content="`分母：有金额字段的 ${data.summary?.missing_currency_denominator ?? 0} 份；缺失计数可互相重叠`" placement="top">
              <el-icon class="tip"><QuestionFilled /></el-icon>
            </el-tooltip>
          </div>
          <div class="mini-value warn">{{ data.summary?.missing_currency_count ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card shadow="hover" class="mini-card">
          <div class="mini-label">
            未知币种金额
            <el-tooltip content="金额有效但币种缺失：单列，按口径不提供合计" placement="top">
              <el-icon class="tip"><QuestionFilled /></el-icon>
            </el-tooltip>
          </div>
          <div class="mini-value warn">{{ data.summary?.unknown_currency_amount_count ?? 0 }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- ===== 口径提示 ===== -->
    <div v-if="data.warnings?.length" class="warn-list">
      <el-alert
        v-for="w in data.warnings"
        :key="w.code"
        :type="w.code.startsWith('missing') ? 'info' : 'warning'"
        :closable="false"
        show-icon
        :title="w.message"
        class="warn-item"
      />
    </div>

    <!-- ===== 图表区 ===== -->
    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never" class="chart-card">
          <template #header>
            <div class="card-header">
              <span>月度文档数量</span>
              <span class="card-subtitle">点击柱体查看该月明细</span>
            </div>
          </template>
          <div v-if="hasMonthly" ref="monthlyRef" class="chart-box" />
          <el-empty v-else description="当前筛选无数据" :image-size="60" />
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never" class="chart-card">
          <template #header>
            <div class="card-header">
              <span>类型数量分布</span>
              <span class="card-subtitle">点击某一类型查看明细</span>
            </div>
          </template>
          <div v-if="typeTotal > 0" ref="typeRef" class="chart-box" />
          <el-empty v-else description="当前筛选无数据" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="row-gap">
      <el-col :span="12">
        <el-card shadow="never" class="chart-card">
          <template #header>
            <div class="card-header">
              <span>月度金额（发票 / 合同分别展示）</span>
              <el-select v-model="amountCurrency" size="small" style="width: 110px">
                <el-option v-for="c in amountCurrencies" :key="c" :label="c" :value="c" />
              </el-select>
            </div>
          </template>
          <template v-if="amountMonths.length">
            <div class="card-subtitle block-note">
              币种 {{ amountCurrency }}；发票与合同分别统计，不做跨类型合计。点击柱体查看该月金额明细
            </div>
            <div ref="amountRef" class="chart-box" />
          </template>
          <el-empty v-else :description="amountCurrencies.length ? '该币种当前无金额数据' : '当前筛选无金额数据'" :image-size="60" />
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never" class="chart-card">
          <template #header>
            <div class="card-header">
              <span>交易对象金额 Top 10</span>
              <el-select v-model="rankRole" size="small" style="width: 150px" @change="load">
                <el-option label="发票·购买方" value="buyer" />
                <el-option label="发票·销售方" value="seller" />
                <el-option label="合同·甲方" value="party_a" />
                <el-option label="合同·乙方" value="party_b" />
              </el-select>
            </div>
          </template>
          <template v-if="rankItems.length">
            <div class="card-subtitle block-note">
              角色：{{ rankRoleLabel }} —— 一次只统计一个角色，不把双方重复累计成总额。点击某一对象查看其明细
            </div>
            <div ref="rankRef" class="chart-box" />
          </template>
          <el-empty v-else description="当前角色与筛选下无排行数据" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <!-- ===== 明细抽屉 ===== -->
    <el-drawer v-model="drawer.visible" :title="drawer.title" size="60%">
      <div v-if="drawer.note" class="drawer-note">{{ drawer.note }}</div>
      <div class="drawer-summary">
        共 <b>{{ drawer.total }}</b> 条
        <span v-for="t in drawer.totals" :key="t.currency" class="drawer-total">
          · {{ t.currency || '币种缺失' }} 合计
          <b>{{ t.amount_total === null ? '不提供（币种缺失）' : t.amount_total }}</b>
          （{{ t.doc_count }} 条）
        </span>
      </div>
      <el-table :data="drawer.items" size="small" border stripe max-height="560">
        <el-table-column prop="doc_id" label="ID" width="60" />
        <el-table-column prop="file_name" label="文件名" min-width="160" show-overflow-tooltip />
        <el-table-column prop="doc_type_label" label="类型" width="70" />
        <el-table-column prop="business_date" label="业务日期" width="110">
          <template #default="{ row }">
            {{ row.business_date || '日期未知' }}
          </template>
        </el-table-column>
        <el-table-column label="金额" width="130" align="right">
          <template #default="{ row }">
            <span v-if="row.amount !== null">{{ row.amount }}</span>
            <span v-else class="muted">缺失</span>
          </template>
        </el-table-column>
        <el-table-column prop="currency" label="币种" width="80">
          <template #default="{ row }">
            {{ row.currency || '—' }}
          </template>
        </el-table-column>
        <el-table-column prop="party" label="交易对象" min-width="120" show-overflow-tooltip />
        <el-table-column label="校对" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.reviewed ? 'success' : 'info'">
              {{ row.reviewed ? '已校对' : '未校对' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="revision" label="版本" width="70" />
      </el-table>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { Search, RefreshLeft } from '@element-plus/icons-vue'
import { getAnalytics, getAnalyticsDetails } from '../api/index.js'
import { ElMessage } from 'element-plus'
// ECharts 按需引入：只打包用到的图表与组件，构建产物里不含任何 CDN 资源
import * as echarts from 'echarts/core'
import { BarChart, PieChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([BarChart, PieChart, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])

// ==================== 过滤条件 ====================
const CURRENCY_PRESET = ['CNY', 'USD', 'EUR', 'JPY', 'HKD', 'UNKNOWN']

const filters = reactive({
  docType: null,
  dateUnknownMode: 'all',   // all / only / exclude
  currency: null,
  reviewed: null,           // 'true' / 'false'（字符串避免空值歧义）
})
const dateRange = ref(null)
const amountCurrency = ref('CNY')
const rankRole = ref('buyer')

const data = reactive({
  summary: null, monthly: [], type_counts: [], amounts: [],
  party_ranking: { items: [] }, warnings: [],
})

function queryParams() {
  const p = {}
  if (filters.docType) p.doc_type = filters.docType
  if (dateRange.value && dateRange.value.length === 2) {
    p.date_from = dateRange.value[0]
    p.date_to = dateRange.value[1]
  }
  if (filters.dateUnknownMode === 'only') p.date_unknown = true
  if (filters.dateUnknownMode === 'exclude') p.date_unknown = false
  if (filters.currency) p.currency = filters.currency
  if (filters.reviewed) p.reviewed = filters.reviewed === 'true'
  p.party_role = rankRole.value
  return p
}

async function load() {
  try {
    const res = await getAnalytics(queryParams())
    const d = res.data
    data.summary = d.summary
    data.monthly = d.monthly || []
    data.type_counts = d.type_counts || []
    data.amounts = d.amounts || []
    data.party_ranking = d.party_ranking || { items: [] }
    data.warnings = d.warnings || []
    // 当前选中币种若在新数据里已不存在，自动切过去，避免图空着让人以为坏了
    if (amountCurrencies.value.length && !amountCurrencies.value.includes(amountCurrency.value)) {
      amountCurrency.value = amountCurrencies.value[0]
    }
    await nextTick()
    renderCharts()
  } catch (e) {
    ElMessage.error('统计加载失败')
  }
}

function resetFilters() {
  filters.docType = null
  filters.dateUnknownMode = 'all'
  filters.currency = null
  filters.reviewed = null
  dateRange.value = null
  load()
}

const currencyOptions = computed(() => {
  const set = new Set(CURRENCY_PRESET)
  ;(data.amounts || []).forEach(g => { if (g.currency) set.add(g.currency) })
  set.add('UNKNOWN')
  return [...set].map(c => ({ value: c, label: c === 'UNKNOWN' ? '币种缺失' : c }))
})

// ==================== 派生数据（与后端同一口径） ====================
const TYPE_COLOR = { invoice: '#409EFF', contract: '#67C23A', other: '#909399' }

const hasMonthly = computed(() => (data.monthly || []).length > 0)
const typeTotal = computed(() => (data.type_counts || []).reduce((s, t) => s + t.doc_count, 0))
const amountCurrencies = computed(() => [...new Set(
  (data.amounts || []).filter(g => g.currency).map(g => g.currency))].sort())

const amountMonths = computed(() => {
  const cur = amountCurrency.value
  const groups = (data.amounts || []).filter(g => g.currency === cur)
  if (!groups.length) return []
  const months = new Set()
  groups.forEach(g => (g.monthly || []).forEach(m => months.add(m.month)))
  const order = [...months].sort((a, b) => {
    if (a === 'unknown') return 1
    if (b === 'unknown') return -1
    return a < b ? -1 : 1
  })
  return order.map(m => ({
    month: m,
    invoice: _monthAmount(groups, 'invoice', m),
    contract: _monthAmount(groups, 'contract', m),
  }))
})

function _monthAmount(groups, docType, month) {
  const g = groups.find(x => x.doc_type === docType)
  if (!g) return null
  const row = (g.monthly || []).find(m => m.month === month)
  return row ? Number(row.amount_total) : null
}

const rankRoleLabel = computed(() => data.party_ranking?.role_label || '-')
const rankItems = computed(() => {
  const items = data.party_ranking?.items || []
  const cur = amountCurrency.value
  // 同一张图只画一个币种：不同币种金额不可比，更不可相加
  return items.filter(i => i.currency === cur)
})

// ==================== ECharts 渲染 ====================
const monthlyRef = ref(null)
const typeRef = ref(null)
const amountRef = ref(null)
const rankRef = ref(null)
const charts = {}

const BASE_TEXT = { color: '#606266', fontSize: 11 }
const AXIS_LINE = { lineStyle: { color: '#ebeef5' } }

function ensure(key, el) {
  if (!el) {
    if (charts[key]) { charts[key].dispose(); delete charts[key] }
    return null
  }
  if (charts[key]) return charts[key]
  const c = echarts.init(el)
  charts[key] = c
  return c
}

function renderCharts() {
  try {
    renderMonthly()
    renderType()
    renderAmount()
    renderRank()
  } catch (e) {
    // 图表是增强，失败不能连累统计数字本身已经显示出来
    ElMessage.error('图表渲染失败，请刷新重试')
  }
}

function renderMonthly() {
  const c = ensure('monthly', monthlyRef.value)
  if (!c) return
  c.off('click')
  c.setOption({
    grid: { left: 44, right: 12, top: 26, bottom: 24 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
               valueFormatter: v => `${v} 份` },
    xAxis: {
      type: 'category',
      data: data.monthly.map(m => m.month === 'unknown' ? '日期未知' : m.month),
      axisLabel: BASE_TEXT, axisLine: AXIS_LINE, axisTick: { show: false },
    },
    yAxis: { type: 'value', minInterval: 1, axisLabel: BASE_TEXT,
             splitLine: { lineStyle: { color: '#f2f4f7' } } },
    series: [{
      type: 'bar', barMaxWidth: 34, itemStyle: { color: '#409EFF', borderRadius: [3, 3, 0, 0] },
      label: { show: true, position: 'top', ...BASE_TEXT },
      data: data.monthly.map(m => m.doc_count),
      cursor: 'pointer',
    }],
  }, true)
  c.on('click', p => {
    const row = data.monthly[p.dataIndex]
    if (row) openDetails({ month: row.month })
  })
}

function renderType() {
  const c = ensure('type', typeRef.value)
  if (!c) return
  c.off('click')
  c.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} 份 ({d}%)' },
    legend: { bottom: 0, textStyle: BASE_TEXT, itemWidth: 10, itemHeight: 10 },
    series: [{
      type: 'pie',
      radius: ['42%', '66%'],
      center: ['50%', '45%'],
      avoidLabelOverlap: true,
      label: { formatter: '{b}\n{c} 份', ...BASE_TEXT },
      cursor: 'pointer',
      data: (data.type_counts || [])
        .filter(t => t.doc_count > 0)
        .map(t => ({ name: t.label, value: t.doc_count, type: t.doc_type,
                     itemStyle: { color: TYPE_COLOR[t.doc_type] } })),
    }],
  }, true)
  c.on('click', p => {
    if (p.data?.type) openDetails({ doc_type: p.data.type })
  })
}

function renderAmount() {
  const c = ensure('amount', amountRef.value)
  if (!c) return
  c.off('click')
  const rows = amountMonths.value
  c.setOption({
    grid: { left: 60, right: 12, top: 30, bottom: 24 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
               valueFormatter: v => (v === null ? '无数据' : `${v} ${amountCurrency.value}`) },
    legend: { top: 0, textStyle: BASE_TEXT, itemWidth: 10, itemHeight: 10 },
    xAxis: {
      type: 'category',
      data: rows.map(r => r.month === 'unknown' ? '日期未知' : r.month),
      axisLabel: BASE_TEXT, axisLine: AXIS_LINE, axisTick: { show: false },
    },
    yAxis: { type: 'value', axisLabel: BASE_TEXT,
             splitLine: { lineStyle: { color: '#f2f4f7' } } },
    series: [
      { name: '发票', type: 'bar', barMaxWidth: 24, cursor: 'pointer',
        itemStyle: { color: '#409EFF', borderRadius: [3, 3, 0, 0] },
        data: rows.map(r => r.invoice) },
      { name: '合同', type: 'bar', barMaxWidth: 24, cursor: 'pointer',
        itemStyle: { color: '#67C23A', borderRadius: [3, 3, 0, 0] },
        data: rows.map(r => r.contract) },
    ],
  }, true)
  c.on('click', p => {
    const row = rows[p.dataIndex]
    if (!row) return
    // 发票/合同分列：点到哪一根就穿透哪一类
    const docType = p.seriesName === '合同' ? 'contract' : 'invoice'
    openDetails({ month: row.month, doc_type: docType, require_amount: true })
  })
}

function renderRank() {
  const c = ensure('rank', rankRef.value)
  if (!c) return
  c.off('click')
  const rows = [...rankItems.value]
    .filter(i => i.amount_total !== null)
    .sort((a, b) => Number(b.amount_total) - Number(a.amount_total))
    .slice(0, 10)
  c.setOption({
    grid: { left: 96, right: 56, top: 12, bottom: 20 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
               valueFormatter: v => `${v} ${amountCurrency.value}` },
    xAxis: { type: 'value', axisLabel: BASE_TEXT,
             splitLine: { lineStyle: { color: '#f2f4f7' } } },
    yAxis: {
      type: 'category', inverse: true,
      data: rows.map(r => r.party),
      axisLabel: { ...BASE_TEXT, width: 88, overflow: 'truncate' },
      axisLine: AXIS_LINE, axisTick: { show: false },
    },
    series: [{
      type: 'bar', barMaxWidth: 16, cursor: 'pointer',
      itemStyle: { color: '#E6A23C', borderRadius: [0, 3, 3, 0] },
      label: { show: true, position: 'right', formatter: '{c}', ...BASE_TEXT },
      data: rows.map(r => Number(r.amount_total)),
    }],
  }, true)
  c.on('click', p => {
    const row = rows[p.dataIndex]
    if (row) openDetails({ party_role: rankRole.value, party: row.party, require_amount: true })
  })
}

function resizeCharts() {
  Object.values(charts).forEach(c => { try { c.resize() } catch (e) { /* 已销毁 */ } })
}
const _onResize = () => resizeCharts()

// ==================== 明细穿透 ====================
const drawer = reactive({ visible: false, title: '', note: '', items: [], total: 0, totals: [] })

async function openDetails(extra) {
  const params = { ...queryParams(), ...extra }
  const parts = []
  if (extra.month) parts.push(`月份=${extra.month === 'unknown' ? '日期未知' : extra.month}`)
  if (extra.doc_type) {
    parts.push(`类型=${{ invoice: '发票', contract: '合同', other: '其他' }[extra.doc_type]}`)
  }
  if (extra.party) parts.push(`交易对象=${extra.party}`)
  if (extra.require_amount) parts.push('仅金额与币种有效')
  drawer.title = `统计明细（${parts.join('，') || '当前筛选'}）`
  drawer.note = extra.require_amount
    ? '金额图点开的明细要求金额与币种都有效，条数与合计与图上一致（同源统计，前端不重算）'
    : '明细与图表使用同一套过滤条件'
  drawer.visible = true
  try {
    const res = await getAnalyticsDetails({ ...params, page: 1, page_size: 200 })
    drawer.items = res.data.items
    drawer.total = res.data.total
    drawer.totals = res.data.amount_totals
  } catch (e) {
    ElMessage.error('明细加载失败')
  }
}

onMounted(() => {
  window.addEventListener('resize', _onResize)
  load()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', _onResize)
  Object.values(charts).forEach(c => { try { c.dispose() } catch (e) { /* noop */ } })
})

// 币种切换不改变后端口径，只需重画金额/排行两张图
watch(amountCurrency, async () => {
  await nextTick()
  renderAmount()
  renderRank()
})

defineExpose({ refresh: load })
</script>

<style scoped>
.analytics-panel { padding-bottom: 8px; }
.card-header { display: flex; align-items: center; gap: 8px; font-weight: 600; }
.card-subtitle { font-weight: 400; font-size: 12px; color: #909399; }
.block-note { display: block; margin: -4px 0 0; line-height: 1.6; }
.filter-card { margin-bottom: 12px; }
.filter-form { margin-top: 4px; }
.filter-note { margin-top: 4px; }

.stat-row { margin-bottom: 12px; }
.mini-card { text-align: center; }
.mini-label {
  font-size: 12px;
  color: #909399;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 3px;
}
.mini-value { font-size: 22px; font-weight: 600; color: #303133; margin-top: 4px; }
.mini-value.warn { color: #E6A23C; }
.tip { color: #C0C4CC; cursor: help; }

.warn-list { margin-bottom: 12px; }
.warn-item { margin-bottom: 6px; }

.row-gap { margin-top: 16px; }
.chart-card { height: 100%; }
.chart-box { width: 100%; height: 260px; }

.drawer-note { font-size: 12px; color: #909399; margin-bottom: 10px; line-height: 1.6; }
.drawer-summary { margin-bottom: 10px; font-size: 13px; color: #606266; }
.drawer-total { margin-left: 6px; }
.muted { color: #C0C4CC; }
</style>
