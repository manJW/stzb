'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const context=vm.createContext({});
vm.runInContext(source.slice(source.indexOf('const colors='),source.indexOf('const captureNames=')),context);
vm.runInContext(`
 const mapTerrain={data:new Uint8Array(100),size:10};
 const state={area:[1,1,10,10],land_tiles:[{coord:[3,4],relation:'ally'}],forts:[]};
 const map={width:100,height:100,scale:20,points:[]};
 const fills=[],borders=[],paths=[],saves=[];let pen=[];
 const cells=()=>fills.filter(c=>['#f0f2f5','#dceffc','#eee5d8'].includes(c.color));
 function project([x,y]){return {x:x*10,y:y*10};}
 function unproject(x,y){return [x/10,y/10];}
 function isFocusedTarget(){return false;}
 const ctx={fillStyle:'',strokeStyle:'',lineWidth:1,
   save(){saves.push({fillStyle:this.fillStyle,strokeStyle:this.strokeStyle,lineWidth:this.lineWidth});},
   restore(){Object.assign(this,saves.pop());},setLineDash(){},
   beginPath(){pen=[];},arc(){throw new Error('Terrain must use squares');},
   moveTo(...args){pen.push({op:'M',args});},lineTo(...args){pen.push({op:'L',args});},
   bezierCurveTo(...args){pen.push({op:'C',args});},
   stroke(){paths.push({color:this.strokeStyle,pen:[...pen]});},
   strokeRect(x,y,width,height){borders.push({width,height,color:this.strokeStyle});},
   fillRect(x,y,width,height){fills.push({point:[(x+width/2)/10,(y+height/2)/10],color:this.fillStyle,width,height});}};
 mapTerrain.data[(2-1)*10+3-1]=1;
 mapTerrain.data[(4-1)*10+2-1]=3;
`,context);
vm.runInContext(source.slice(source.indexOf('function mapTarget('),source.indexOf('function drawMapFort(')),context);
vm.runInContext(source.slice(source.indexOf('function drawMapCell('),source.indexOf('function drawMapCoordinates(')),context);
vm.runInContext('drawMapTerrain();state.land_tiles.forEach(drawMapLand)',context);
assert.equal(vm.runInContext("cells().find(d=>d.point.join(',')==='2,3').color",context),'#dceffc');
assert.equal(vm.runInContext("cells().find(d=>d.point.join(',')==='4,2').color",context),'#eee5d8');
assert.equal(vm.runInContext("cells().find(d=>d.point.join(',')==='3,3').color",context),'#f0f2f5');
assert.equal(vm.runInContext("cells().some(d=>d.point.join(',')==='3,4')",context),false);
assert.ok(vm.runInContext("borders.some(b=>b.color==='#276c7d')",context));
assert.equal(vm.runInContext('map.points.length',context),1,'Background cells must not take army clicks');
assert.equal(vm.runInContext('paths.length',context),0,'Zoomed-out terrain must only use colors');
// Terrain outside the scene cannot reappear when the user zooms in for textures.
vm.runInContext('fills.length=0;paths.length=0;map.scale=40;state.area=[3,3,6,6];drawMapTerrain()',context);
assert.equal(vm.runInContext('cells().every(d=>d.point[0]>=3&&d.point[0]<=6&&d.point[1]>=3&&d.point[1]<=6)',context),true);
assert.equal(vm.runInContext("cells().some(d=>d.color==='#dceffc'||d.color==='#eee5d8')",context),false);
assert.equal(vm.runInContext('cells().length',context),15);
assert.equal(vm.runInContext('paths.length',context),0);
vm.runInContext('fills.length=0;state.area=null;drawMapTerrain()',context);
assert.equal(vm.runInContext('cells().length',context),0,'Unknown scene bounds must not expand to the whole map');
vm.runInContext(`fills.length=0;map.points.length=0;
 drawMapLand({coord:[5,5],relation:'unknown'});drawMapLand({coord:[6,5],relation:'neutral'});
 drawMapLand({coord:[2,3],relation:'neutral'});drawMapLand({coord:[7,5],relation:'enemy'});`,context);
assert.equal(vm.runInContext('cells().length',context),2,'Unowned river must retain its river style');
assert.equal(vm.runInContext("cells().every(d=>d.color==='#f0f2f5'&&d.width===16&&d.height===16)",context),true);
assert.equal(vm.runInContext('borders.at(-1).color',context),'#af433c');
vm.runInContext(`{
  for(const scale of [6,12,20,23.9,24,50]){
    map.scale=scale;fills.length=0;borders.length=0;paths.length=0;map.points.length=0;
    drawMapLand({coord:[7,5],relation:'enemy'});
    for(const kind of ['neutral','water','mountain'])drawMapCell([5,5],kind,true);
    const occupied=borders.find(border=>border.color==='#af433c');
    if(cells().some(cell=>cell.width!==occupied.width||cell.height!==occupied.height))
      throw new Error('Terrain and occupied land must stay the same size at every zoom');
    if(map.points.slice(1).some(point=>point.radius>6))
      throw new Error('Larger cells must not expand their click area over nearby armies');
    const waves=paths.filter(p=>p.color==='#398bb0'),peaks=paths.filter(p=>p.color==='#675344');
    if(scale<24&&paths.length)throw new Error('Textures must be hidden when zoomed out');
    if(scale>=24){
      if(waves.length!==2||waves.some(p=>p.pen.filter(c=>c.op==='C').length!==2))
        throw new Error('River must use two static wave strokes');
      if(peaks.length!==1||peaks[0].pen.filter(c=>c.op==='L').length!==4)
        throw new Error('Mountain must use a double-peak stroke');
      const half=occupied.width/2;
      for(const p of paths)for(const command of p.pen)for(const value of command.args)
        if(value<50-half||value>50+half)throw new Error('Texture must stay inside its tile');
    }
    if(saves.length)throw new Error('Terrain drawing must restore canvas styles');
  }
}`,context);
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
assert.ok(html.includes('<svg class="legend-terrain water"'));
assert.ok(html.includes('<svg class="legend-terrain mountain"'));
console.log('Terrain palettes, zoom-dependent static textures, equal sizes, scene clipping and click isolation passed');
