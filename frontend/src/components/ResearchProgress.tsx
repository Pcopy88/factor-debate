type Row = Record<string, any>;
const roles:Row={value:'价值 / 质量',growth:'成长 / 财务动量',engineer:'因子工程师'};

export function ResearchProgress({summary,hypotheses}:{summary:Row|null,hypotheses:Row[]}){
 if(!summary)return null;
 const title=(id:string)=>hypotheses.find(h=>h.id===id)?.payload.title||id.slice(0,8);
 if(summary.legacy)return <div className="information-banner"><div><strong>旧版研究档案</strong><p>原始记录完整保留。新建任务将使用完整反馈、交替实证辩论与相似性筛选。</p></div></div>;
 const rounds=summary.round_manifest||[];
 const selection=summary.candidate_selection?.[0]?.payload;
 return <section className="panel research-v2"><div className="section-head"><div><h2>回测后的交叉研究</h2><p>初次评估 → 对方批判与接手改写 → 重测 → 交换角色。最终测试成绩不会进入这里。</p></div><span className="pill">{summary.definition_count} 个已登记定义</span></div>
  <div className="research-meta">历史参考 {summary.reference_count} 项 · {summary.reference_cutoff?'参考集已冻结':'数据验收后冻结参考集'} · 三个固定基准单独比较</div>
  {!rounds.length&&<p className="research-empty">批准第 5 环节后开始初次评估。默认最多两轮实证改写。</p>}
  <div className="research-rounds">{rounds.map((r:Row)=><article key={r.id} className="research-round"><strong>{r.payload.round_no===0?'初次评估':`第 ${r.payload.round_no} 轮重测`}</strong><span>{r.payload.hypothesis_ids.length} 个版本 · 反馈已归档</span></article>)}</div>
  {(summary.lineages||[]).map((line:Row)=><details key={line.lineage_id} className="research-lineage"><summary><strong>{title(line.lineage_id)}</strong><span>{roles[line.origin_role]} 起源 → {roles[line.current_author]} 当前作者</span></summary>
   <p>当前版本：{title(line.head_id)} · {line.stop_reason||'按批准的轮次与预算执行'}</p>
   {(summary.research_feedback||[]).filter((f:Row)=>f.payload.lineage_id===line.lineage_id).map((f:Row)=>{const h=hypotheses.find(h=>h.id===f.payload.hypothesis_id);return <div className="research-version" key={f.id}><strong>第 {f.payload.round_no} 轮 · {roles[h?.role]} · {title(f.payload.hypothesis_id)}</strong><code>{f.payload.formula}</code>
    {(summary.empirical_critique||[]).filter((c:Row)=>c.payload.target_id===f.payload.hypothesis_id).map((c:Row)=><p key={c.id}><b>{roles[c.role]} 的批判：</b>{c.payload.issue}<br/>拟检验：{c.payload.proposed_test}</p>)}
    {(summary.empirical_revision||[]).filter((c:Row)=>c.payload.parent_id===f.payload.hypothesis_id).map((c:Row)=><div key={c.id}><p><b>处理与预期：</b>{c.payload.rationale}</p><ul>{c.payload.expected_effects.map((s:string,i:number)=><li key={i}>{s}</li>)}</ul>{c.payload.unresolved_issues.length>0&&<p>未解决：{c.payload.unresolved_issues.join('；')}</p>}</div>)}
   </div>})}
  </details>)}
  {selection?.stop_reason&&<p className="research-stop">{selection.stop_reason}</p>}
 </section>;
}
