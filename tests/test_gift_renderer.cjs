const assert = require('node:assert/strict');
const {resolve, text} = require('../server/static/gift-renderer.js');
const catalog = [
    {asset: '001_Rose.png', names: ['Rose', 'ดอกกุหลาบ'], verifiedIds: ['9001']},
    {asset: '002_Dove.png', names: ['White Dove'], verifiedIds: ['9002']}
];
const msg = (name, id) => ({platform:'tiktok', eventType:'gift', giftName:name, giftId:id, giftCount:3});
assert.equal(resolve(msg('White Dove', '9001'), catalog).asset, '001_Rose.png');
assert.equal(resolve(msg('  ＲＯＳＥ  '), catalog).asset, '001_Rose.png');
assert.equal(resolve(msg('ดอกกุหลาบ'), catalog).asset, '001_Rose.png');
assert.equal(resolve(msg('White    Dove'), catalog).asset, '002_Dove.png');
assert.equal(resolve(msg('ros'), catalog), null);
assert.equal(resolve(msg('unknown', '001'), catalog), null);
assert.equal(resolve({...msg('Rose'), platform:'youtube'}, catalog), null);
assert.equal(resolve(msg('Rose'), [...catalog, {asset:'003.png',names:['rose'],verifiedIds:[]}]), null);
assert.equal(resolve(msg('Rose','9001'), [...catalog, {asset:'003.png',names:[],verifiedIds:['9001']}]), null);
assert.equal(resolve(msg('Rose'), [{asset:'../evil.png',names:['Rose'],verifiedIds:[]}]), null);
assert.equal(resolve({...msg('unknown'),giftImage:'javascript:alert(1)'}, catalog), null);
assert.equal(text(msg('Rose')), 'Rose ×3');
assert.equal(text({eventType:'comment',comment:'hello'}), 'hello');
console.log('Gift resolver: 13 assertions passed');
// Exercise the real browser helper while the catalog fetch is still pending.
const vm = require('node:vm');
const fs = require('node:fs');
class Element {
    constructor() { this.children=[]; this.style={}; this.isConnected=true; this.handlers={}; }
    prepend(child) { this.children.unshift(child); }
    append(child) { this.children.push(child); }
    replaceChildren() { this.children=[]; this.textContent=''; }
    addEventListener(name, handler) { this.handlers[name]=handler; }
}
let complete;
const browser = {document:{createElement:()=>new Element()},fetch:()=>new Promise(done=>{complete=done;}),window:{},Promise,Number};
vm.runInNewContext(fs.readFileSync(require.resolve('../server/static/gift-renderer.js'),'utf8'),browser);
const holder = new Element();
browser.window.GiftRenderer.decorate(holder,msg('White Dove'));
assert.equal(holder.children[0].textContent,'🎁');
complete({ok:true,json:async()=>({entries:catalog})});
browser.window.GiftRenderer.ready.then(()=>{
    const marker=holder.children[0];
    assert.equal(marker.children[0].src,'/gifts/002_Dove.png');
    marker.children[0].handlers.error();
    assert.equal(marker.textContent,'🎁');
    assert.equal(marker.children.length,0);
    console.log('Gift DOM: delayed catalog load and broken image fallback passed');
}).catch(error=>{console.error(error);process.exitCode=1;});
