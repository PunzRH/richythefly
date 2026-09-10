import json, re, time, sqlite3, math
from collections import Counter
from PIL import Image, ImageDraw, ImageFont
rows=[json.loads(l) for l in open('runs/live/events.jsonl')]
db=sqlite3.connect('file:runs/live/ledger.sqlite?mode=ro',uri=True)
meta={k:json.loads(v) for k,v in db.execute("select key,value from meta")}
harv=[]
for ln in open('runs/richy_keeper.log'):
    m=re.search(r"^(\S+ \S+) harvest sent \S+ fly_eth=([\d.]+) status=1",ln)
    if m: harv.append((time.mktime(time.strptime(m.group(1),"%Y-%m-%d %H:%M:%S")), float(m.group(2))))
W,H=2400,1500; bg=(16,15,13); gold=(214,168,84); ink=(236,230,218); dim=(160,152,138); red=(220,90,90); green=(110,200,130); teal=(120,190,200)
im=Image.new("RGB",(W,H),bg); d=ImageDraw.Draw(im)
def F(sz,bold=False):
    try: return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf", sz)
    except Exception: return ImageFont.load_default()
t0=rows[0]['wall_time']; t1=rows[-1]['wall_time']; span=max(t1-t0,1)
start=float(rows[0]['equity_usdc']); eq=[(r['wall_time'],float(r['equity_usdc'])) for r in rows]
fee_cum=[]; acc=0.0
for t,a in sorted(harv):
    acc+=a; fee_cum.append((t,acc))
fees_total=acc
d.text((60,40),f"RICHY — FIRST {int(span/60)} MINUTES LIVE, TICK BY TICK",font=F(46,True),fill=ink)
d.text((60,100),f"Every point is a real tick of the connectome; every marker a real on-chain transaction from Richy's wallet 0x9673…3609 on Robinhood Chain. Start: {start:.4f} ETH.",font=F(21),fill=dim)
# main chart: equity + fee income
cx,cy,cw,ch=80,170,1560,620
d.rounded_rectangle((cx,cy,cx+cw,cy+ch),radius=10,outline=(84,74,58),width=2,fill=(22,21,18))
vals=[v for _,v in eq]+[v for _,v in fee_cum]+[start]
lo,hi=min(vals)*0.95,max(vals)*1.05
def X(t): return cx+40+(max(t,t0)-t0)/span*(cw-80)
def Y(v): return cy+ch-40-(v-lo)/(hi-lo)*(ch-90)
for k in range(6):
    v=lo+(hi-lo)*k/5; y=Y(v); d.line((cx+40,y,cx+cw-40,y),fill=(40,37,32)); d.text((cx+cw-36,y-8),f"{v:.3f}",font=F(14),fill=dim)
d.line([(X(t),Y(v)) for t,v in eq],fill=ink,width=3)
if fee_cum:
    pts=[(X(t0),Y(0))]+[(X(t),Y(v)) for t,v in fee_cum]+[(X(t1),Y(fee_cum[-1][1]))]
    d.line(pts,fill=gold,width=3)
d.line((X(t0),Y(start),X(t1),Y(start)),fill=(90,84,74))
d.text((cx+44,Y(start)-20),"starting capital",font=F(14),fill=dim)
# trade markers
for r in rows:
    e=r['execution']; s=e.get('status')
    if s in ('FILLED','SETTLED'):
        x,y=X(r['wall_time']),Y(float(r['equity_usdc'])); col=green if r['neural']['side']=='BUY' else red
        d.ellipse((x-7,y-7,x+7,y+7),fill=col); d.text((x+9,y-22),r['product'].split('-')[0][:9],font=F(13,True),fill=col)
d.text((cx+20,cy+12),"white = equity (cash + positions at executable bid; the vertical step is fee income landing in his wallet)   gold = cumulative $RICHY fee income   green = BUY   red = SELL",font=F(16),fill=dim)
d.text((cx+20,cy+ch-28),f"{time.strftime('%H:%M',time.localtime(t0))} UTC+2",font=F(14),fill=dim); d.text((cx+cw-120,cy+ch-28),f"{time.strftime('%H:%M',time.localtime(t1))}",font=F(14),fill=dim)
# right panel: numbers
px,py,pw,ph=1680,170,660,620
d.rounded_rectangle((px,py,px+pw,py+ph),radius=10,outline=(84,74,58),width=2,fill=(22,21,18))
fills=[r for r in rows if r['execution'].get('status') in ('FILLED','SETTLED')]
buys=sum(1 for r in fills if r['neural']['side']=='BUY'); sells=len(fills)-buys
vet=Counter(re.sub(r"\(.*","",r['execution'].get('reason','')).strip()[:38] for r in rows if r['execution'].get('status')=='VETO')
holds=sum(1 for r in rows if r['execution'].get('status')=='HOLD')
realized=sum(float(r['execution'].get('realized_pnl_eth') or 0) for r in fills)
end=float(rows[-1]['equity_usdc'])
lines=[("TICKS",f"{len(rows)}"),("REAL TRADES",f"{len(fills)}  ({buys} buys / {sells} sells)"),("HOLDS",f"{holds}"),("VETOES",f"{sum(vet.values())}"),("EQUITY NOW",f"{end:.4f} ETH"),("FEE INCOME",f"{fees_total:.4f} ETH  ({len(harv)} harvests)"),("TRADING P&L",f"{end-start-fees_total:+.4f} ETH"),("REALIZED (closed)",f"{realized:+.4f} ETH"),("SYNAPSES CHANGED",f"{rows[-1]['neural']['memory']['changed_edges']:,}")]
yy=py+18
for k,v in lines:
    d.text((px+20,yy),k,font=F(15),fill=dim); d.text((px+20,yy+20),v,font=F(26,True),fill=gold if 'FEE' in k else ink); yy+=62
# vetoes + taste
bx,by,bw,bh=80,830,1100,600
d.rounded_rectangle((bx,by,bx+bw,by+bh),radius=10,outline=(84,74,58),width=2,fill=(22,21,18))
d.text((bx+20,by+14),"WHY HE DIDN'T TRADE (vetoes are his guardrails and his nose)",font=F(22,True),fill=gold)
yy=by+56; mxv=max(vet.values()) if vet else 1
for k,n in vet.most_common(8):
    d.text((bx+20,yy),k,font=F(17),fill=ink); w=int((bw-420)*n/mxv); d.rectangle((bx+400,yy+4,bx+400+w,yy+18),fill=teal); d.text((bx+410+w,yy),str(n),font=F(16),fill=dim); yy+=32
yy+=10
d.text((bx+20,yy),"WHAT HE HAS LEARNED (taste memory: mean return per smell bucket)",font=F(22,True),fill=gold); yy+=40
taste=meta.get('taste') or {}
for k,v in sorted(taste.items(), key=lambda kv: kv[1]['sum']/max(kv[1]['n'],1)):
    mean=v['sum']/max(v['n'],1); col=red if mean<0 else green
    d.text((bx+20,yy),f"{k:44s}",font=F(16),fill=ink); d.text((bx+560,yy),f"{mean*100:+.0f}% avg over {v['n']} trade(s)",font=F(16,True),fill=col); yy+=26
burned=meta.get('burned') or {}
if burned: d.text((bx+20,yy+8),f"Burned (off his radar 6 h): {', '.join(c[:10]+'…' for c in burned)}",font=F(16),fill=red)
# smell legend
sx,sy,sw,sh=1220,830,1120,600
d.rounded_rectangle((sx,sy,sx+sw,sy+sh),radius=10,outline=(84,74,58),width=2,fill=(22,21,18))
d.text((sx+20,sy+14),"HIS NOSE, AFTER TONIGHT'S LESSONS",font=F(22,True),fill=gold)
txt=["INSIDER: 137 real buyers, no whale, fake score 0. Bought 3×, curve drained 4 min later. Sold at −77%. Lesson booked to the bucket 'new | hot | outflow | crowd | spread'. Coin burned for 6 h.",
"PAWCKET: a bundler staircase, 21 buys from 8 wallets, same-sized clips, zero sells. Ramp detector now scores it 0.5, fake 0.86 → nose veto on BUY.",
"New senses since 21:30: deployer bag (> 5 % of supply), rug (curve below 30 % of its peak), ramp shape (uniform clips, machine cadence, no sells), burn memory.",
"Guardrails on top of free reign: max 25 % of cash per order; never add to a position down > 30 %; nose veto when fake ≥ 0.5.",
"He is not restricted to new launches: the hot list is chain-wide by activity, any age, curve or graduated pool.",
"Still true: an organic crowd can rug and a bot ramp can be a real launch. He will be wrong again. The taste memory and the synapses are the part that changes."]
yy=sy+50
def wrap(t,f,w):
    out=[];line=""
    for word in t.split(" "):
        tt=(line+" "+word).strip()
        if f.getlength(tt)<=w: line=tt
        else: out.append(line); line=word
    out.append(line); return out
for t in txt:
    for l in wrap(t,F(17),sw-40): d.text((sx+20,yy),l,font=F(17),fill=ink); yy+=23
    yy+=8
d.text((60,1455),"richythefly.com · @RichyTheFly · every trade verifiable on chain · not financial advice",font=F(18),fill=dim)
im.save("site/richy-session.png"); print("saved", len(rows), "ticks", len(fills), "fills", "fees", round(fees_total,4))
