'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),nodes=new Map();
function element(id){if(!nodes.has(id))nodes.set(id,{textContent:'',style:{},dataset:{}});return nodes.get(id);}
const context=vm.createContext({URLSearchParams,location:{search:''},localStorage:{getItem:()=>null},
  CSSStyleSheet:class {replaceSync(text){this.text=text;}},
  document:{getElementById:id=>id==='clientRelationColors'?nodes.get(id):element(id),createElement:()=>({textContent:''}),
    adoptedStyleSheets:[],head:{appendChild:n=>nodes.set(n.id,n)}},fetch:()=>new Promise(()=>{}),setTimeout(){},clearTimeout(){}});
vm.runInContext(source.split("$('settingsButton').onclick")[0],context);
vm.runInContext(`
  const styles={};for(const name of ['self','union','other','enemy','bandit','friend_other','friend_bandit'])
    styles[name]={inside:'#30b8e5',outside:'#85d8ea',icon:'#64dcff'};
  styles.self={inside:'#92d719',outside:'#c9f16b',icon:'#abc374'};
  styles.friend_bandit={inside:'#58389b',outside:'#85d8ea',icon:'#73459a'};
  styles.bandit={inside:'#58389b',outside:'#4f396e',icon:'#73459a'};
  const palette={status:'ready',source:'client_map_palette',user_id:'123',own_union_id:'88',color_mode:0,
    last_seen_at:999,styles};
  state={server_now:1000,config:{self:{user_id:'123',union_id:'88'}},health:{status:'live',relation_colors:palette}};
  syncClientRelationColors();
  if(factionGroups.own.color!==softenGameColor('#30b8e5')||factionGroups.friendly_wanderer.border!==softenGameColor('#85d8ea'))throw Error('Map palette drift');
  if(styles.union.inside!=='#30b8e5'||styles.self.inside!=='#92d719')throw Error('Raw client palette was mutated');
  for(const hex of ['#f83d3d','#92d719','#30b8e5','#ffc061','#7ee2f6','#58389b','#892500']){
    const rgb=softenGameColor(hex).slice(1).match(/../g).map(n=>parseInt(n,16)/255);
    const hi=Math.max(...rgb),lo=Math.min(...rgb),l=(hi+lo)/2,s=(hi-lo)/(1-Math.abs(2*l-1));
    if(l<.395||l>.615||s>.47)throw Error('Palette remains too vivid: '+hex);
  }
  if(factionGroups.enemy_wanderer.color!==factionGroups.wanderer.color)throw Error('Invented hostile bandit palette');
  if(ownArmyColor!==softenGameColor('#92d719')||!factionClass({relation:'self'}).includes('faction-self'))throw Error('Self palette drift');
  if(!clientPaletteSheet.text.includes('--faction-fill:'+softenGameColor('#58389b'))||
    !clientPaletteSheet.text.includes('.faction-own.faction-self'))throw Error('Queue/legend drift');
  palette.color_mode=1;palette.styles.union={inside:'#123456',outside:'#abcdef',icon:'#fedcba'};
  syncClientRelationColors();if(factionGroups.own.color!==softenGameColor('#123456'))throw Error('Color mode did not follow');
  palette.user_id='other';syncClientRelationColors();
  if(factionGroups.own.color!==defaultFactionStyles.own.color||clientPaletteSheet.text)throw Error('Previous role palette leaked');
  palette.user_id='123';syncClientRelationColors();
  state.server_now=1100;syncClientRelationColors();if(clientSelfPalette)throw Error('Stale palette stayed active');
  state.server_now=1000;palette.styles.union.inside='red;}body{display:none';syncClientRelationColors();
  if(clientSelfPalette)throw Error('Invalid CSS accepted');
`,context);
assert.equal(vm.runInContext('document.adoptedStyleSheets.length',context),1);
console.log('Client relation colors: map, queues, legend, mode, role and stale guards passed');
