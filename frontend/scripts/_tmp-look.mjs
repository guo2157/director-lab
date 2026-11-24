import { chromium } from 'playwright-core'
const MULTI=[{base_url:'https://a.example.com:8443/',weight:1,enabled:true},{base_url:'http://b.example.net:8188',weight:1,enabled:true}]
const browser = await chromium.launch({ channel:'chrome', headless:true })
for (const [w,h,tag] of [[1440,900,'1440x900'],[1100,900,'1100x900'],[768,1024,'768x1024'],[480,900,'480x900'],[390,844,'390x844']]) {
  const ctx = await browser.newContext({ viewport:{width:w,height:h}, deviceScaleFactor:2 })
  const page = await ctx.newPage()
  await page.route('**/api/events*', r=>r.abort())
  await page.route('**/api/settings/comfyui-instances', r=>r.fulfill({status:200,contentType:'application/json',body:JSON.stringify(MULTI)}))
  await page.goto('http://127.0.0.1/',{waitUntil:'domcontentloaded'})
  await page.waitForSelector('.comfy-entry',{timeout:15000}); await page.waitForTimeout(800)
  const m = await page.evaluate(()=>{
    const R=s=>{const e=document.querySelector(s);return e?e.getBoundingClientRect():null}
    const e=document.querySelector('.comfy-entry'), cs=getComputedStyle(e)
    const lab=document.querySelector('.comfy-label')
    const ink=(el)=>{if(!el)return null;const r=document.createRange();r.selectNodeContents(el);const b=r.getBoundingClientRect();return {w:+b.width.toFixed(1),h:+b.height.toFixed(1)}}
    const pill=R('.header-pill'), nav=R('.top-nav'), brand=R('.brand'), btn=R('.comfy-entry')
    return {
      btn:`${Math.round(btn.width)}x${Math.round(btn.height)}`,
      fs:cs.fontSize, fw:cs.fontWeight,
      labelVisible: lab?getComputedStyle(lab).display!=='none':false,
      labelInk: ink(lab),
      labelColor: lab?getComputedStyle(lab).color:null,
      accentColor: (()=>{const a=document.querySelector('.comfy-label-accent');return a?getComputedStyle(a).color:null})(),
      markBox: (()=>{const b=R('.comfy-mark');return b?`${Math.round(b.width)}x${Math.round(b.height)}`:null})(),
      navOff: Math.round(nav.x+nav.width/2-(pill.x+pill.width/2)),
      leftGap: Math.round(brand.x-pill.x), rightGap: Math.round(pill.right-btn.right),
      overflowX: document.documentElement.scrollWidth>document.documentElement.clientWidth+1,
      clipped: [...document.querySelectorAll('.header-pill button')].filter(b=>{const r=b.getBoundingClientRect();return r.width>0&&r.right>innerWidth+1}).length,
    }
  })
  console.log(tag.padEnd(10), JSON.stringify(m))
  await page.screenshot({ path:`../docs/uiux/screenshots/round13/header__${tag}.png`, clip:{x:0,y:0,width:w,height:150} })
  await ctx.close()
}
await browser.close()
