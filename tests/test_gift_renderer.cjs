const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {resolve, allowedURL, text} = require('../server/static/gift-renderer.js');
const url='https://p16-webcast.tiktokcdn.com/img/rose.webp';
const gift={platform:'tiktok',eventType:'gift',giftName:'Rose',giftCount:10,giftId:'5655',giftImageUrl:url};
assert.equal(resolve(gift),url);
assert.equal(text(gift),'Rose ×10');
assert.equal(resolve({...gift,giftName:'White Dove',giftImageUrl:null}),null);
assert.equal(resolve({...gift,platform:'youtube'}),null);
for(const value of [null,{},true,'javascript:alert(1)','http://tiktokcdn.com/a','https://evil/a','https://tiktokcdn.com.evil/a','https://eviltiktokcdn.com/a','https://u@tiktokcdn.com/a','https://tiktokcdn.com:443/a','https://tiktokcdn.com\\@evil/a','https://tiktokcdn.com/a\n','https://tiktokcdn.com/'+ 'a'.repeat(2048)]) assert.equal(allowedURL(value),null);
function element(){return {style:{},children:[],textContent:'',prepend(x){this.children.unshift(x)},append(x){this.children.push(x)},replaceChildren(){this.children=[]},addEventListener(name,fn){this[name]=fn}};}
const browser={URL,document:{createElement:element},fetch(){throw Error('No catalog fetch allowed')}};
vm.runInNewContext(fs.readFileSync(require.resolve('../server/static/gift-renderer.js'),'utf8'),browser);
let container=element(); browser.GiftRenderer.decorate(container,gift);
let marker=container.children[0]; assert.equal(marker.children[0].src,url);
marker.children[0].error(); assert.equal(marker.textContent,'🎁'); assert.equal(marker.children.length,0);
for(const msg of [{...gift,giftName:'White Dove',giftImageUrl:null},{...gift,giftImageUrl:'https://evil/a'},{...gift,platform:'youtube'}]){
 container=element();browser.GiftRenderer.decorate(container,msg);assert.equal(container.children[0].textContent,'🎁');assert.equal(container.children[0].children.length,0);
}
console.log('Gift renderer remote, missing, malicious, imageerror, YouTube and no-fetch checks passed');
