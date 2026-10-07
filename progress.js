/* Personal application records: browser-only, separate from the public recruitment database. */
const JobProgress = (() => {
  const KEY = 'jobRadarProgress:v1';
  const KIND = 'job-radar-personal-progress';
  const STATUSES = ['待处理', '已投递', '待确认', '不适合'];
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const safeURL = value => { try { const u = new URL(value); return ['https:', 'http:'].includes(u.protocol) ? u.href : ''; } catch { return ''; } };
  let records = Object.create(null), loadWarning = '', config, editingId = null;
  const dateOK = value => typeof value === 'string' && Number.isFinite(Date.parse(value));
  function validate(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) throw Error('记录格式不正确');
    const out = Object.create(null), entries = Object.entries(input);
    if (entries.length > 5000) throw Error('记录数量过多');
    for (const [id, r] of entries) {
      if (!id || id.length > 300 || ['__proto__','prototype','constructor'].includes(id) || !r || typeof r !== 'object') throw Error('岗位编号不正确');
      if (!STATUSES.includes(r.status) || typeof r.note !== 'string' || r.note.length > 2000 || !dateOK(r.updatedAt)) throw Error('状态、备注或日期不正确');
      if (!r.job || typeof r.job !== 'object' || typeof r.job.title !== 'string' || typeof r.job.org !== 'string') throw Error('缺少岗位名称或单位');
      const job = {id, title:r.job.title.slice(0,500), org:r.job.org.slice(0,500), city:String(r.job.city || '未说明').slice(0,200), url:safeURL(r.job.url), deadline:typeof r.job.deadline === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(r.job.deadline) ? r.job.deadline : null};
      out[id] = {status:r.status, note:r.note, updatedAt:new Date(r.updatedAt).toISOString(), job};
    }
    return out;
  }
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) records = validate(JSON.parse(raw));
  } catch { loadWarning = '浏览器记录无法读取（可能被禁用或格式损坏）。原数据未删除；请先导出备份或恢复浏览器存储后再保存。'; }
  function persist(next) {
    try { localStorage.setItem(KEY, JSON.stringify(next)); }
    catch { throw Error('没有保存成功：浏览器存储不可用或空间不足。请勿关闭本页，先检查隐私设置或导出已有记录。'); }
    records = next;
  }
  const get = j => records[j.id];
  const filter = (j, value) => value === '全部' || (get(j)?.status || '待处理') === value;
  function panel(j) {
    const r = get(j), status = r?.status || '待处理';
    return `<section class="personal-progress" aria-label="我的求职记录"><div class="progress-row"><span class="progress-badge ${status==='已投递'?'applied':status==='不适合'?'rejected':status==='待确认'?'pending':''}">我的标记：${escape(status)}</span><button type="button" class="btn progress-edit" data-id="${escape(j.id)}">${r?'修改标记 / 备注':'标记 / 备注'}</button></div>${r?.note?`<div class="progress-note">${escape(r.note)}</div>`:''}${r?`<small>记录时间：${escape(new Date(r.updatedAt).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}))}</small>`:''}${j.personalArchive?'<div class="progress-note">当前数据库已不展示此岗位，以下仅为个人历史记录，不能据此判断仍在招聘。详细条件请回查原公告。</div>':''}</section>`;
  }
  function jobs(current, normalize) {
    const currentIDs = new Set(current.map(j=>j.id));
    return [...current.filter(j=>get(j)), ...Object.entries(records).filter(([id])=>!currentIDs.has(id)).map(([id,r])=>normalize({...r.job,id,personalArchive:true,verificationStatus:'个人历史记录',sourceLevel:'仅保存岗位标识；不代表当前仍在招',summary:'个人记录中的历史岗位。当前岗位库已不展示，报名状态与资格请查原公告。',match:'未评估'}))];
  }
  function announce(message) { document.querySelector('#progressNotice').textContent = message; }
  function updateSummary() {
    const count = Object.values(records), live = new Set(config.getJobs().map(j=>j.id));
    document.querySelector('#progressSummary').textContent = `已投递 ${count.filter(r=>r.status==='已投递').length} · 待确认 ${count.filter(r=>r.status==='待确认').length} · 不适合 ${count.filter(r=>r.status==='不适合').length} · 历史记录 ${Object.keys(records).filter(id=>!live.has(id)).length}`;
  }
  function open(id) {
    const job = config.getJobs().find(j=>j.id===id) || records[id]?.job;
    if (!job) return;
    editingId=id;
    document.querySelector('#progressJobTitle').textContent = job.title + ' · ' + job.org;
    document.querySelector('#progressStatus').value = records[id]?.status || '待处理';
    document.querySelector('#progressNote').value = records[id]?.note || '';
    document.querySelector('#progressDelete').hidden = !records[id];
    document.querySelector('#progressError').textContent='';
    document.querySelector('#progressDialog').showModal();
  }
  function save(event) {
    event.preventDefault();
    if (loadWarning) { document.querySelector('#progressError').textContent=loadWarning; return; }
    if (!editingId) return;
    const job = config.getJobs().find(j=>j.id===editingId) || records[editingId]?.job;
    if (!job) return;
    const next = Object.assign(Object.create(null), records), status = document.querySelector('#progressStatus').value;
    next[editingId] = {status, note:document.querySelector('#progressNote').value.trim(), updatedAt:new Date().toISOString(), job:{id:editingId,title:job.title,org:job.org,city:job.city||'未说明',url:safeURL(job.url),deadline:job.deadline||null}};
    try { persist(validate(next)); }
    catch (e) { document.querySelector('#progressError').textContent=e.message; return; }
    document.querySelector('#progressDialog').close();
    config.rerender();
    announce('已保存：' + status + '。可通过求职进度筛选，或在“我的记录”中查看。');
  }
  function remove() {
    if (!editingId || !confirm('清除此岗位的个人标记和备注？招聘信息不会删除。')) return;
    const next = Object.assign(Object.create(null),records); delete next[editingId];
    try { persist(next); } catch(e) { document.querySelector('#progressError').textContent=e.message; return; }
    document.querySelector('#progressDialog').close(); config.rerender(); announce('个人标记已清除，岗位恢复为待处理。');
  }
  function download() {
    try {
      if (loadWarning) throw Error(loadWarning);
      const blob = new Blob([JSON.stringify({kind:KIND,version:1,exportedAt:new Date().toISOString(),records},null,2)],{type:'application/json'});
      const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='地理雷达-我的求职记录-'+new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'})+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
      announce('已导出个人记录。备份文件可能含私人备注，请自行保管，不要上传公开仓库。');
    } catch(e) { announce(e.message); }
  }
  async function importFile(event) {
    const file=event.target.files?.[0];event.target.value='';if(!file)return;
    try {
      if(loadWarning)throw Error(loadWarning);
      if(file.size>2*1024*1024)throw Error('文件超过2MB，未导入');
      const data=JSON.parse(await file.text());if(data.kind!==KIND||data.version!==1)throw Error('请选用本地理雷达导出的记录文件，不能导入岗位数据库或财会记录');
      const incoming=validate(data.records), next=Object.assign(Object.create(null),records);let changed=0;
      for(const [id,r]of Object.entries(incoming))if(!next[id]||Date.parse(r.updatedAt)>Date.parse(next[id].updatedAt)){next[id]=r;changed++;}
      if(!changed){announce('没有需要合并的新记录，当前较新的标记已保留。');return;}
      if(!confirm('将合并 '+changed+' 条个人记录；相同岗位保留记录时间较新的版本。是否继续？'))return;
      persist(next);config.rerender();announce('已导入 '+changed+' 条记录。没有更改招聘数据库。');
    } catch(e){announce('导入失败，原记录未更改：'+e.message);}
  }
  function init(options) {
    config=options;
    document.addEventListener('click',event=>{const button=event.target.closest('.progress-edit');if(button)open(button.dataset.id);});
    document.querySelector('#progressForm').addEventListener('submit',save);
    document.querySelector('#progressCancel').onclick=()=>document.querySelector('#progressDialog').close();
    document.querySelector('#progressDelete').onclick=remove;
    document.querySelector('#exportProgress').onclick=download;
    document.querySelector('#importProgress').onclick=()=>document.querySelector('#progressFile').click();
    document.querySelector('#progressFile').onchange=importFile;
    window.addEventListener('storage',event=>{
      if(event.key!==KEY)return;
      try { records=event.newValue?validate(JSON.parse(event.newValue)):Object.create(null);loadWarning='';config.rerender();announce('已同步同一浏览器其他页面的个人标记。'); }
      catch { announce('另一页面的记录格式异常，未覆盖当前记录。'); }
    });
    if(loadWarning)announce(loadWarning);
  }
  function readFavorites() {
    try { const value=JSON.parse(localStorage.getItem('savedJobs')||'[]');return Array.isArray(value)?value.filter(v=>typeof v==='string'):[]; } catch { return []; }
  }
  function saveFavorites(value) {
    try {localStorage.setItem('savedJobs',JSON.stringify(value));return true;}
    catch {announce('收藏未保存：浏览器存储不可用。');return false;}
  }
  return {init,panel,filter,jobs,updateSummary,readFavorites,saveFavorites};
})();
