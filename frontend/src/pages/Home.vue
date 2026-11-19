<template>
  <div>
    <div class="hero">
      <div>
        <h1>总览</h1>
        <p>当前到货日三腿行数、原批与订正批。原批封印后当日预报/实收/回执不可再改，订正只进订正批。</p>
      </div>
      <div class="row">
        <input v-model="day" placeholder="到货日" />
        <button @click="load()">刷新</button>
      </div>
    </div>
    <p class="msg">{{ err }}</p>
    <div class="grid" v-if="d">
      <div class="stat"><div class="k">到货日</div><div class="v">{{ d.dock_day }}</div></div>
      <div class="stat"><div class="k">预报 / 实收 / 回执</div><div class="v">{{ d.expect_count }} / {{ d.receive_count }} / {{ d.confirm_count }}</div></div>
      <div class="stat"><div class="k">未决差额</div><div class="v">{{ d.pending_variance }}</div></div>
      <div class="stat">
        <div class="k">结案</div>
        <div class="v"><span class="tag" :class="d.sealed ? 'seal' : 'bad'">{{ d.sealed ? '已结案' : '未结案' }}</span></div>
      </div>
    </div>
    <div class="cols" v-if="d">
      <div class="panel">
        <h2>原批</h2>
        <template v-if="d.baseline_batch">
          <p>
            {{ d.baseline_batch.batch_no }} · {{ d.baseline_batch.status }} ·
            对齐 {{ d.baseline_batch.matched_count }} · 差额 {{ d.baseline_batch.variance_count }}
          </p>
          <p>
            指纹 <code>{{ short(d.baseline_batch.fingerprint) }}</code>
            <template v-if="d.baseline_batch.sealed_fingerprint">
              <span class="tag" :class="d.baseline_batch.intact ? 'ok' : 'bad'">
                {{ d.baseline_batch.intact ? '封印完好' : '封印指纹已变' }}
              </span>
            </template>
          </p>
          <router-link class="link" :to="`/reconcile?day=${d.dock_day}&batch=${d.baseline_batch.id}`">打开原批对账 →</router-link>
        </template>
        <p v-else class="hint">当日还没有对账批次。</p>
      </div>
      <div class="panel">
        <h2>订正批</h2>
        <template v-if="d.correction_batch">
          <p>
            {{ d.correction_batch.batch_no }} · {{ d.correction_batch.status }} ·
            对齐 {{ d.correction_batch.matched_count }} · 差额 {{ d.correction_batch.variance_count }}
          </p>
          <p>
            关联原批 {{ d.correction_batch.corrects_batch_no }} ·
            订正 {{ d.correction_batch.correction_count }} 行 ·
            物化 {{ d.correction_batch.materialized_at || '未物化' }}
          </p>
          <router-link class="link" :to="`/reconcile?day=${d.dock_day}&batch=${d.correction_batch.id}`">打开订正批 →</router-link>
        </template>
        <p v-else class="hint">原批封印后若发现错数，可在三联对账页开订正批。</p>
      </div>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const d = ref(null)
const day = ref('')
const err = ref('')
function short(fp) { return fp ? `${fp.slice(0, 12)}…` : '' }
async function load() {
  err.value = ''
  try {
    const q = day.value ? `?dock_day=${encodeURIComponent(day.value)}` : ''
    d.value = await api(`/dashboard${q}`)
    day.value = d.value.dock_day
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
