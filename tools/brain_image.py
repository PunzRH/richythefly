import json, glob
from PIL import Image, ImageDraw, ImageFont
n=json.load(open('runs/neurons.json')); xyz=n["xyz"]; cls=n["cls"]; classes=n["classes"]; groups=n["groups"]
sp=None
for f in sorted(glob.glob('runs/live_old_*/spikes.json')+glob.glob('runs/live/spikes.json')): sp=json.load(open(f))
W,H=2400,1500; bg=(16,15,13); gold=(214,168,84); ink=(236,230,218); dim=(160,152,138); red=(214,90,90)
im=Image.new("RGB",(W,H),bg); d=ImageDraw.Draw(im)
def F(sz,bold=False):
    try: return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf", sz)
    except Exception: return ImageFont.load_default()
def wrap(text, font, width):
    out=[]; line=""
    for word in text.split(" "):
        t=(line+" "+word).strip()
        if font.getlength(t)<=width: line=t
        else: out.append(line); line=word
    out.append(line); return out
d.text((60,50),"INSIDE RICHY — THE REAL BRAIN, THE REAL SPIKES, THE MATH",font=F(48,True),fill=ink)
d.text((60,110),"Left: all 141,781 neurons with a known soma position in the MaleCNS v1.0 connectome, at their real coordinates (dorsal view). Right: one actual 500 ms decision window from Richy's live run.",font=F(21),fill=dim)
bx,by,bw,bh=60,170,1090,760
pts=[(i,p) for i,p in enumerate(xyz) if p!=[0,0,0]]
xs=[p[0] for _,p in pts]; ys=[p[1] for _,p in pts]
mnx,mxx,mny,mxy=min(xs),max(xs),min(ys),max(ys); s=min(bw/(mxx-mnx),bh/(mxy-mny))
def P(p): return (bx+(p[0]-mnx)*s, by+(p[1]-mny)*s)
colors={i:((70,110,160) if (c.startswith("ol_") or c.startswith("visual")) else (80,78,74) if c.startswith("vnc") else (170,120,60) if (c.startswith("cb_") or c=="ENS") else (120,110,95)) for i,c in enumerate(classes)}
for i,p in pts: d.point(P(p),fill=colors[cls[i]])
def hl(idxs,col,r):
    for i in idxs:
        p=xyz[i]
        if p==[0,0,0]: continue
        x,y=P(p); d.ellipse((x-r,y-r,x+r,y+r),fill=col)
hl(groups["kc"],(90,200,120),2); hl(groups["reward"],(255,210,80),6); hl(groups["aversive"],(255,80,80),7); hl(groups["decoder_left"]+groups["decoder_right"],(120,200,255),7)
lx,ly=70,by+bh+10
for col,label in [((70,110,160),"optic lobes / visual"),((170,120,60),"central brain"),((80,78,74),"ventral nerve cord"),((90,200,120),"4,064 Kenyon cells — the memory"),((255,210,80),"15 × PAM11 dopamine reward cells"),((255,80,80),"2 × PPL101 dopamine punishment cells"),((120,200,255),"DNp20 L / R — the read-out (buy / sell)")]:
    d.ellipse((lx,ly+4,lx+12,ly+16),fill=col); d.text((lx+20,ly),label,font=F(17),fill=ink); ly+=22
ly+=14
for t,col in [("How to read it: the two big blue lobes are the optic lobes, where the chart enters as light. The green cloud is the mushroom body, the fly's associative memory: profit and loss physically change synapses there. Yellow and red dots are the 17 dopamine neurons we stimulate. The light-blue pair are DNp20, the descending neurons that turn a real fly left or right; here they turn Richy into a buyer or a seller.",ink),
             ("Sources: MaleCNS v1.0 flat connectome (body annotations, synapse weights at ≥ 0.5 confidence, neurotransmitter predictions); stonkfly simulation kernel (nftechie); Robinhood Chain contracts read via JSON-RPC.",dim),
             ("Not financial advice. Real money, real risk. A fly brain has never traded profitably; the point is to watch whether this one learns to.",red)]:
    for l in wrap(t,F(18),1090): d.text((70,ly),l,font=F(18),fill=col); ly+=24
    ly+=8
rx,ry,rw,rh=1220,170,1120,470
d.rounded_rectangle((rx,ry,rx+rw,ry+rh),radius=10,outline=(84,74,58),width=2,fill=(22,21,18))
d.text((rx+16,ry+12),"REAL SPIKES — one decision window (500 ms of brain time)",font=F(22,True),fill=gold)
if sp:
    top=sp["top"][:600]; mx=max(c for _,c in top)
    d.text((rx+16,ry+42),f"tick {sp['tick']} · {sp['total_spikes']:,} spikes · {sp['active_cells']:,} active cells · decision {sp['decision']} · DNp20 L {sp['left_hz']} Hz / R {sp['right_hz']} Hz · {sp['plastic_edges_changed']:,} synapses changed",font=F(16),fill=dim)
    x0=rx+20; y0=ry+72; colw=(rw-40)/len(top); maxh=rh-130
    av=set(groups["aversive"]); rw_=set(groups["reward"]); kc=set(groups["kc"])
    for k,(idx,c) in enumerate(top):
        h=max(1,int(c/mx*maxh)); col=(255,80,80) if idx in av else (255,210,80) if idx in rw_ else (90,200,120) if idx in kc else (200,190,170)
        d.rectangle((x0+k*colw,y0+maxh-h,x0+(k+1)*colw,y0+maxh),fill=col)
    d.text((rx+16,ry+rh-44),f"Spike count of the 600 most active neurons in this window, sorted (max {mx} spikes = {mx*2} Hz). Green = Kenyon cell, yellow = PAM11, red = PPL101, grey = other.",font=F(15),fill=dim)
    d.text((rx+16,ry+rh-24),"Every bar is a real neuron in the connectome and a real count from the simulation; nothing is drawn for effect.",font=F(15),fill=dim)
ex,ey,ew,eh=1220,670,1120,780
d.rounded_rectangle((ex,ey,ex+ew,ey+eh),radius=10,outline=(84,74,58),width=2,fill=(22,21,18))
d.text((ex+16,ey+12),"THE MATH",font=F(22,True),fill=gold)
lines=[("Neuron dynamics (leaky integrate-and-fire, every one of 166,700 cells):",dim),
("   τ dV/dt = −(V − V_rest) + R·(Σ_j w_ij · s_j(t) + I_ext)      spike when V ≥ V_th, then V ← V_reset",ink),
("   w_ij signed by transmitter: ACh / Glu excitatory, GABA inhibitory; magnitude = synapse count from the connectome",ink),("",ink),
("Sensory drive:  I_ext(R1–R6 cell k) ~ luminance of the chart pixel mapped to ommatidium k (3,335 mapped inputs)",ink),("",ink),
("Decision (DNp20 read-out over the 500 ms window):",dim),
("   rate_L, rate_R = spikes / 0.5 s;  gate = spikes(DNpe017)",ink),
("   action = BUY if rate_R > rate_L and gate > 0;  SELL if rate_L > rate_R and gate > 0;  else HOLD",ink),
("   conviction c = clamp( 3 · |rate_R − rate_L| / (rate_R + rate_L), 0.05, 1 )      order = c · cash (buy)  or  c · position (sell)",ink),("",ink),
("Reinforcement (engineered stimulus, biological rule):",dim),
("   Δ = equity_t − anchor_(t−1)  (marked to executable bid, fees and gas included)",ink),
("   Δ ≥ +δ → 200 ms current into PAM11 (×15);   Δ ≤ −δ → 200 ms current into PPL101 (×2);   δ = 0.2 % of capital",ink),
("   Plasticity on KC→MBON07/11 edges:  w ← w · (1 − η · r_KC · r_DAN)   coincident Kenyon-cell and dopamine firing depresses the synapse",ink),("",ink),
("Foraging (which coin to look at):",dim),
("   scent_i = log1p(50·inflow_i) + 0.6·log1p(20·raised_i) + 0.4·progress_i + 0.5·log1p(buyers_i) + 0.6·log1p(trades_i)",ink),
("   fake_i = 0.5·[buyers ≤ 2 and buys ≥ 3] + 0.8·max(0, topShare − 0.4) + 0.3·[bundled/buys > 0.5]",ink),
("   P(choose i) ~ exp( scent_i − 2·fake_i + taste(bucket_i) + U(0, 0.8) )      taste = clamp(3 · mean return of that smell bucket, ±1.5) · min(1, n/5)",ink),("",ink),
("Execution guards:  minOut = size · limit,  limit = ask·1.08 (buy) or bid·0.92 (sell);  curve impact ≤ 3 % (Pons rule);  8 % drift veto;  gas reserve 0.0005 ETH",ink)]
yy=ey+48
for t,col in lines: d.text((ex+16,yy),t,font=F(17,True) if col==dim else F(17),fill=col); yy+=27
im.save("site/richy-brain.png"); print("saved")
