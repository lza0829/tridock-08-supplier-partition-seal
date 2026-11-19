<template>
  <div>
    <div class="hero">
      <div>
        <h1>三联对账</h1>
        <p>按供应商+货号+到货日对齐三数量；不一致进差额。未决差额清零后可结案。</p>
      </div>
    </div>

    <div class="panel">
      <div class="row">
        <input v-model="day" placeholder="到货日" />
        <button @click="run">跑三联对账</button>
        <button class="warn" @click="seal">结案封印</button>
        <button class="secondary" @click="load()">刷新</button>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
      <div class="batches" v-if="batches.length">
        <button
          v-for="b in batches"
          :key="b.id"
          :class="['batchchip', { active: b.id === batchId, corr: b.kind === 'correction' }]"
          @click="selectBatch(b.id)"
        >
          {{ b.batch_no }}<span v-if="b.kind === 'correction'">（订正）</span> · {{ b.status }}
        </button>
      </div>
      <p v-if="batch">
        批次 {{ batch.batch_no }} · {{ batch.kind === 'correction' ? '订正批' : '原批' }} ·
        对齐 {{ batch.matched_count }} · 差额 {{ batch.variance_count }} · {{ batch.status }}
      </p>
      <p v-if="batch && batch.corrects_batch_no">
        订正关联原批：
        <a href="#" @click.prevent="jumpTo(batch.corrects_batch_id)">{{ batch.corrects_batch_no }}</a>
      </p>
      <p v-if="batch && batch.related_corrections && batch.related_corrections.length">
        关联订正批：
        <a
          v-for="c in batch.related_corrections"
          :key="c.id"
          href="#"
          class="reflink"
          @click.prevent="jumpTo(c.id)"
        >{{ c.batch_no }}（{{ c.status }}）</a>
      </p>
    </div>

    <div class="panel" v-if="batch">
      <h2>封印快照</h2>
      <p>
        指纹 <code>{{ short(batch.fingerprint) }}</code>
        <template v-if="batch.sealed_fingerprint">
          · 封印指纹 <code>{{ short(batch.sealed_fingerprint) }}</code>
          <span class="tag" :class="batch.intact ? 'ok' : 'bad'">
            {{ batch.intact ? '与封印一致' : '封印指纹已变' }}
          </span>
        </template>
        <span v-else class="tag">尚未封印</span>
      </p>
      <table v-if="snapshots.length">
        <thead><tr><th>阶段</th><th>指纹</th><th>时间</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="s in snapshots" :key="s.id">
            <td>{{ s.stage }}</td><td><code>{{ short(s.fingerprint) }}</code></td>
            <td>{{ s.captured_at }}</td><td>{{ s.note || '' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="baselineSealed">
      <h2>订正批</h2>
      <p class="hint">
        原批已封印，明细只读。订正不写进原批，而是挂靠原日的独立订正批：写订正行 → 物化对齐 → 处置差额 → 结案。
      </p>
      <div class="row">
        <button @click="openCorrection">开订正批</button>
        <template v-if="correction">
          <button class="secondary" :disabled="correction.status !== 'open'" @click="materialize">物化对齐</button>
          <button class="warn" :disabled="correction.status !== 'open'" @click="sealCorrection">订正结案</button>
        </template>
      </div>
      <template v-if="correction">
        <p>
          订正批 {{ correction.batch_no }} · {{ correction.status }} ·
          关联原批 {{ correction.corrects_batch_no }} ·
          物化 {{ correction.materialized_at || '未物化' }}
        </p>
        <div class="row" v-if="correction.status === 'open'">
          <input v-model="form.correction_no" placeholder="订正号" />
          <input v-model="form.supplier_code" placeholder="供应商" />
          <input v-model="form.sku" placeholder="货号" />
          <select v-model="form.leg">
            <option value="expect">预报</option>
            <option value="receive">实收</option>
            <option value="confirm">回执</option>
          </select>
          <input v-model.number="form.new_qty" type="number" placeholder="订正后数量" />
          <input v-model="form.reason" placeholder="原因" />
          <button @click="addCorrection">写订正行</button>
        </div>
        <p v-else class="hint">订正批已结案，明细只读。</p>
        <table v-if="correctionLines.length">
          <thead><tr><th>订正号</th><th>供应商</th><th>货号</th><th>腿</th><th>原值</th><th>订正后</th><th>差额</th><th>原因</th></tr></thead>
          <tbody>
            <tr v-for="l in correctionLines" :key="l.id">
              <td>{{ l.correction_no }}</td><td>{{ l.supplier_code }}</td><td>{{ l.sku }}</td>
              <td>{{ legName(l.leg) }}</td><td>{{ l.prev_qty }}</td><td>{{ l.new_qty }}</td>
              <td>{{ l.delta }}</td><td>{{ l.reason || '' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="hint">还没有订正行。</p>
      </template>
      <p v-else class="hint">原日尚未开订正批。</p>
    </div>

    <div class="panel">
      <h2>对齐行</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>预报</th><th>实收</th><th>回执</th><th>状态</th><th>来源</th></tr></thead>
        <tbody>
          <tr v-for="r in matches" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td><td>{{ r.expect_qty }}</td><td>{{ r.receive_qty }}</td><td>{{ r.confirm_qty }}</td>
            <td><span class="tag" :class="r.aligned ? 'ok' : 'bad'">{{ r.aligned ? '对齐' : '差额' }}</span></td>
            <td>
              <span v-if="r.origin === 'correction'" class="tag seal">订正{{ r.changed ? '·已改' : '' }}</span>
              <span v-else class="tag">原批</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>差额处置</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>三数量</th><th>分桶</th><th>来源</th><th>命运</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in variances" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td>
            <td>{{ r.expect_qty }} / {{ r.receive_qty }} / {{ r.confirm_qty }}</td>
            <td><span class="tag">{{ bucketName(r.bucket) }}</span></td>
            <td>{{ originName(r.origin) }}</td>
            <td>{{ r.fate }}</td>
            <td>
              <button class="secondary" @click="fate(r.id, 'accept_short')">认短</button>
              <button class="secondary" @click="fate(r.id, 'accept_over')">认溢</button>
              <button class="secondary" @click="fate(r.id, 'quarantine')">隔离</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
<script setup>
import { computed, ref, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'

const route = useRoute()
const day = ref(route.query.day || '2026-11-18')
const wantedBatch = ref(route.query.batch ? Number(route.query.batch) : null)
const batches = ref([])
const batchId = ref(null)
const batch = ref(null)
const matches = ref([])
const variances = ref([])
const snapshots = ref([])
const correction = ref(null)
const correctionLines = ref([])
const err = ref('')
const ok = ref('')
const form = ref({ correction_no: '', supplier_code: 'SUP-A', sku: '', leg: 'receive', new_qty: 0, reason: '' })

const baselineSealed = computed(() => {
  const base = batches.value.find((b) => b.kind === 'baseline')
  return !!base && base.status === 'sealed'
})

function short(fp) {
  return fp ? `${fp.slice(0, 12)}…` : ''
}
function legName(leg) {
  return { expect: '预报', receive: '实收', confirm: '回执' }[leg] || leg
}
function bucketName(b) {
  return { leg_low: '单腿偏低', leg_high: '单腿偏高', mixed: '三腿各异', new_key: '订正新增' }[b] || b || '—'
}
function originName(o) {
  return { baseline: '原批', correction: '订正', carried: '沿用原处置' }[o] || o || '—'
}

async function selectBatch(id) {
  const found = batches.value.find((b) => b.id === id)
  if (!found) return
  batchId.value = id
  await loadBatch()
  if (found.kind === 'correction') {
    correction.value = found
    correctionLines.value = await api(`/reconcile/corrections?batch_id=${id}`)
  } else {
    correction.value = batches.value.find((b) => b.kind === 'correction') || null
    correctionLines.value = correction.value
      ? await api(`/reconcile/corrections?batch_id=${correction.value.id}`)
      : []
  }
}

async function loadBatch() {
  if (!batchId.value) {
    batch.value = null; matches.value = []; variances.value = []; snapshots.value = []
    return
  }
  batch.value = await api(`/reconcile/batches/${batchId.value}`)
  matches.value = await api(`/reconcile/matches?batch_id=${batchId.value}`)
  variances.value = await api(`/reconcile/variances?batch_id=${batchId.value}`)
  snapshots.value = await api(`/reconcile/batches/${batchId.value}/snapshots`)
}

async function load(preferredKind) {
  err.value = ''
  batches.value = await api(`/reconcile/batches?dock_day=${encodeURIComponent(day.value)}`)
  correction.value = batches.value.find((b) => b.kind === 'correction') || null
  let target = wantedBatch.value ? batches.value.find((b) => b.id === wantedBatch.value) : null
  if (!target) target = batches.value.find((b) => b.kind === preferredKind)
  if (!target) target = batches.value.find((b) => b.kind === 'correction') || batches.value[0]
  wantedBatch.value = null
  if (target) {
    await selectBatch(target.id)
  } else {
    batchId.value = null
    await loadBatch()
    correctionLines.value = []
  }
}

async function jumpTo(id) {
  await selectBatch(id)
}

async function run() {
  err.value = ''; ok.value = ''
  try {
    const res = await api('/reconcile/run', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = `对账完成：${res.batch_no}`
    await load('baseline')
  } catch (e) { err.value = e.message }
}

async function seal() {
  err.value = ''; ok.value = ''
  try {
    await api('/reconcile/seal', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '已结案'
    await load('baseline')
  } catch (e) { err.value = e.message }
}

async function openCorrection() {
  err.value = ''; ok.value = ''
  try {
    const res = await api('/reconcile/corrections/open', {
      method: 'POST', body: JSON.stringify({ dock_day: day.value }),
    })
    ok.value = res.reused ? `沿用未结案的订正批 ${res.batch_no}` : `已开订正批 ${res.batch_no}`
    await load('correction')
  } catch (e) { err.value = e.message }
}

async function addCorrection() {
  err.value = ''; ok.value = ''
  if (!correction.value) { err.value = '请先开订正批'; return }
  try {
    await api('/reconcile/corrections', {
      method: 'POST',
      body: JSON.stringify({ batch_id: correction.value.id, ...form.value }),
    })
    ok.value = '订正行已写入订正批（原批未动）'
    form.value.correction_no = ''
    await load('correction')
  } catch (e) { err.value = e.message }
}

async function materialize() {
  err.value = ''; ok.value = ''
  try {
    const res = await api(`/reconcile/corrections/${correction.value.id}/materialize`, { method: 'POST' })
    ok.value = `订正已物化：对齐 ${res.matched_count} · 差额 ${res.variance_count}`
    await load('correction')
  } catch (e) { err.value = e.message }
}

async function sealCorrection() {
  err.value = ''; ok.value = ''
  try {
    await api(`/reconcile/corrections/${correction.value.id}/seal`, { method: 'POST' })
    ok.value = '订正批已结案，原批封印未变'
    await load('correction')
  } catch (e) { err.value = e.message }
}

async function fate(id, f) {
  err.value = ''; ok.value = ''
  try {
    await api(`/reconcile/variances/${id}/fate`, { method: 'POST', body: JSON.stringify({ fate: f }) })
    ok.value = '已更新命运'
    await loadBatch()
  } catch (e) { err.value = e.message }
}

watch(day, () => { wantedBatch.value = null; load() })

onMounted(() => load())
</script>
