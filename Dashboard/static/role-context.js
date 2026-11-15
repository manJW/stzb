'use strict';
(()=>{
 let scope='',busy=false;
 async function refresh(){
  if(busy||document.hidden)return;busy=true;
  try{
   const r=await fetch('/api/role-context',{cache:'no-store'});if(!r.ok)return;const c=await r.json();
   document.body.dataset.rolePending=String(!c.ready);if(!c.ready)return;
   const next=c.context_id||c.profile_id;
   if(scope&&scope!==next){location.reload();return;}scope=next;
   document.body.dataset.statisticsTitle=c.statistics_title;
   for(const link of document.querySelectorAll('a[href="/attendance"]'))link.textContent=c.statistics_title;
   const ally=document.querySelector('#relation option[value="ally"]');if(ally)ally.textContent=c.own_label;
   const ownLegend=document.querySelector('.map-legend .faction-own');
   if(ownLegend?.parentElement?.lastChild)ownLegend.parentElement.lastChild.textContent=c.own_label;
   if(location.pathname==='/attendance'){
    document.querySelector('h1').textContent=c.statistics_title;document.title='率土 · '+c.statistics_title;
    const eyebrow=document.querySelector('.eyebrow');if(eyebrow)eyebrow.textContent=c.identity_label+' · '+(c.has_organization?c.organization_label+'战绩':'个人战绩');
    const group=document.getElementById('group');if(group)group.closest('label').hidden=!c.has_organization;
    const joined=document.querySelector('[data-column="joined_at"]');if(joined)joined.textContent=c.is_wanderer?'加入聚义时间':'进盟时间';
   }else if(location.pathname==='/'){
    document.querySelector('h1').textContent=c.is_wanderer?'流浪军情报':'同盟战场情报';
   }
   const note=document.getElementById('roleScopeNote');if(note)note.textContent=c.report_scope;
  }catch{}finally{busy=false;}
 }
 refresh();setInterval(refresh,10000);document.addEventListener('visibilitychange',refresh);
})();
