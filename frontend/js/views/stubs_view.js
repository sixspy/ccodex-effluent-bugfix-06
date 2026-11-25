export const stubsViewTemplate = `
<!-- BUGFIX-06 修复：外送改判同事务双写，改判面板必须带正式指标三项 -->
  <div class="grid-2">
    <div class="panel">
      <h3>空桩 · 外送化验快递</h3>
      <p class="muted">接口可点，默认 dry_run，未接通外送系统。改判回执与采样正式指标同事务双写。</p>
      <label>采样号</label><input v-model="labForm.sample_code"/>
      <label>快递槽</label><input v-model="labForm.courier_slot"/>
      <div class="actions"><button @click="enqueueLab">演练入队</button></div>
      <table style="margin-top:0.8rem">
        <thead><tr><th>ID</th><th>采样</th><th>槽</th><th>状态</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="j in labJobs" :key="j.id">
            <td>{{ j.id }}</td><td>{{ j.sample_code }}</td><td>{{ j.courier_slot }}</td>
            <td :class="j.status==='已改判'?'pass':'muted'">{{ j.status }}</td>
            <td>{{ j.detail }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h3>外送化验改判（同事务双写）</h3>
      <p class="muted">缺正式指标的改判将被拒写；提交后采样指标、超标页、对账页、血缘页同源刷新。</p>
      <label>空桩任务 ID</label><input type="number" v-model.number="rejudgeForm.job_id"/>
      <div class="form-row">
        <div><label>COD（必填）</label><input type="number" v-model.number="rejudgeForm.cod"/></div>
        <div><label>氨氮（必填）</label><input type="number" v-model.number="rejudgeForm.nh3n"/></div>
      </div>
      <div class="form-row">
        <div><label>总磷（必填）</label><input type="number" v-model.number="rejudgeForm.tp"/></div>
        <div><label>改判说明</label><input v-model="rejudgeForm.detail"/></div>
      </div>
      <div class="actions"><button class="warn" @click="rejudgeLab">提交改判</button></div>
      <pre v-if="rejudgeResult" style="white-space:pre-wrap;font-size:0.82rem;margin-top:0.6rem">{{ JSON.stringify(rejudgeResult, null, 2) }}</pre>
    </div>
    <div class="panel">
      <h3>空桩 · 污泥消化排程</h3>
      <p class="muted">接口可点，默认 dry_run，未接通污泥仓排程。</p>
      <label>厂 ID</label><input type="number" v-model.number="sludgeForm.plant_id"/>
      <label>消化仓</label><input v-model="sludgeForm.digester_bay"/>
      <div class="actions"><button @click="enqueueSludge">演练入队</button></div>
      <table style="margin-top:0.8rem">
        <thead><tr><th>ID</th><th>仓</th><th>模式</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="j in sludgeJobs" :key="j.id"><td>{{ j.id }}</td><td>{{ j.digester_bay }}</td><td>{{ j.mode }}</td><td>{{ j.status }}</td></tr>
        </tbody>
      </table>
    </div>
  </div>`;
