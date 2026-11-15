'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const context=vm.createContext({window:{},document:{getElementById:()=>null},URLSearchParams});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/formations.js'),'utf8'),context);
const catalog=context.window.FormationCatalog;
const heroes=[{position:3,name:'前锋',level:40,red_stars:5},{position:1,name:'<大营>',level:45,red_stars:0},
 {position:2,name:'中军',level:43,red_stars:2,skill_slot_ids:[123,0,null],skills:[{id:123,name:'战法甲',level:10},{id:999,name:'不能填入空槽',level:1}]}];
const html=catalog.roster(heroes);
assert.ok(html.indexOf('&lt;大营&gt;')<html.indexOf('中军'));assert.ok(html.indexOf('中军')<html.indexOf('前锋'));
assert.ok(html.includes('45级'));assert.ok(html.includes('0红'));assert.ok(!html.includes('<大营>'));
const skills=catalog.skillTable(heroes);
assert.ok(skills.includes('空槽'));assert.ok(skills.includes('战法甲'));assert.ok(skills.includes('未取得'));
assert.ok(!skills.includes('不能填入空槽'),'explicit empty slots must not borrow another skill');
const root={classList:{add(){}},addEventListener(){},innerHTML:''};
catalog.render(root,{pending:0,items:[{player_key:'battle:a',player_name:'玩家甲',main_count:1,spartan_count:2,unknown_count:0,at:1}]});
assert.ok(root.innerHTML.includes('主力 1'));assert.ok(root.innerHTML.includes('斯巴达 2'));
assert.ok(!/<details[^>]*\bopen\b/.test(root.innerHTML),'player and team sections start collapsed');
assert.ok(!root.innerHTML.includes('队号'));
console.log('Formation catalog: hero order, red/levels, escaping, explicit empty slots and collapsed sections passed');
