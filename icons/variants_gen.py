import sys, math, random
from PIL import Image, ImageDraw, ImageFilter
import importlib.util
G,C,R,Y=(46,182,126,255),(0,200,246,255),(244,0,88,255),(246,176,0,255)
COL=[G,C,R,Y]
N=1024; S=2
def tile(bg):
    t=Image.new("RGBA",(N,N),(0,0,0,0)); d=ImageDraw.Draw(t)
    off=int(N*.09); sz=N-2*off
    m=Image.new("L",(N*S,N*S),0); ImageDraw.Draw(m).rounded_rectangle([off*S,off*S,(off+sz)*S,(off+sz)*S],radius=int(sz*S*.2237),fill=255)
    m=m.resize((N,N),Image.LANCZOS)
    sh=Image.new("RGBA",(N,N),(0,0,0,0)); sh.paste((0,0,0,90),(0,N//60),m); t.alpha_composite(sh.filter(ImageFilter.GaussianBlur(N//50)))
    if isinstance(bg,tuple) and len(bg)==2:
        g=Image.new("RGBA",(N,N)); gd=ImageDraw.Draw(g)
        for y in range(N): 
            k=y/N; gd.line([0,y,N,y],fill=tuple(int(bg[0][i]*(1-k)+bg[1][i]*k) for i in range(3))+(255,))
    else: g=Image.new("RGBA",(N,N),bg)
    f=Image.new("RGBA",(N,N),(0,0,0,0)); f.paste(g,(0,0),m); t.alpha_composite(f)
    return t,m
def layer(): return Image.new("RGBA",(N*S,N*S),(0,0,0,0))
def fin(t,l,m):
    l=l.resize((N,N),Image.LANCZOS); mm=m; a=Image.composite(l.getchannel("A"),Image.new("L",(N,N),0),mm)
    l.putalpha(a); t.alpha_composite(l); return t
def bars(d,n,w,gap,cx,cy,hs,cols,rad=True,x0=None):
    tot=n*w+(n-1)*gap; x=cx-tot/2
    for i in range(n):
        h=hs[i]; c=cols[i%len(cols)] if not callable(cols) else cols(i)
        d.rounded_rectangle([x*S,(cy-h/2)*S,(x+w)*S,(cy+h/2)*S],radius=(w/2*S if rad else 0),fill=c); x+=w+gap
def wave(n,lo,hi,seed=0):
    return [lo+(hi-lo)*(0.5+0.5*math.sin(i*2.1+seed))*(0.6+0.4*math.sin(i*.7)) for i in range(n)]
def mic(d,cx,cy,r,fg):
    w=int(r*.16)*S
    d.rounded_rectangle([(cx-r*.22)*S,(cy-r*.55)*S,(cx+r*.22)*S,(cy+r*.1)*S],radius=r*.22*S,fill=fg)
    d.arc([(cx-r*.42)*S,(cy-r*.3)*S,(cx+r*.42)*S,(cy+r*.38)*S],0,180,fill=fg,width=w)
    d.line([cx*S,(cy+r*.38)*S,cx*S,(cy+r*.62)*S],fill=fg,width=w)
    d.line([(cx-r*.25)*S,(cy+r*.62)*S,(cx+r*.25)*S,(cy+r*.62)*S],fill=fg,width=w)
def badge(d,color=(40,110,230,255)):
    r=165; cx=N-.09*N-r-30; cy=cx
    d.ellipse([(cx-r-14)*S,(cy-r-14)*S,(cx+r+14)*S,(cy+r+14)*S],fill=(255,255,255,255))
    d.ellipse([(cx-r)*S,(cy-r)*S,(cx+r)*S,(cy+r)*S],fill=color); mic(d,cx,cy,r,(255,255,255,255))
WH=(255,255,255,255); DK=(24,26,38,255)
V={}
def v1(): # white tile, 4 colored bars equalizer
    t,m=tile(WH); l=layer(); d=ImageDraw.Draw(l); bars(d,4,110,52,N/2-40,N/2-40,[300,520,400,230],COL); badge(d); return fin(t,l,m)
def v2(): # white, 7 bars cycling colors wave
    t,m=tile(WH); l=layer(); d=ImageDraw.Draw(l); bars(d,7,70,34,N/2-30,N/2-30,[180,340,500,380,460,280,170],COL); badge(d); return fin(t,l,m)
def v3(): # dark tile, bright bars
    t,m=tile(((28,30,48),(10,11,20))); l=layer(); d=ImageDraw.Draw(l); bars(d,5,100,44,N/2-40,N/2-40,[260,440,560,400,230],[G,C,R,Y,G]); badge(d); return fin(t,l,m)
def v4(): # white, 9 thin bars, symmetrical waveform
    t,m=tile(WH); l=layer(); d=ImageDraw.Draw(l); hs=[120,230,360,500,590,500,360,230,120]; bars(d,9,56,28,N/2-30,N/2-30,hs,lambda i:[G,C,R,Y][min(i,8-i)%4]); badge(d); return fin(t,l,m)
def v5(): # pinwheel-ish: 4 vertical bars of equal height, staggered vertical offsets (steps)
    t,m=tile(WH); l=layer(); d=ImageDraw.Draw(l)
    w=120;g=40;x=N/2-(4*w+3*g)/2-40; offs=[60,-30,-110,-190]
    for i,c in enumerate(COL):
        cy=N/2-40+offs[i]*-1+20; d.rounded_rectangle([x*S,(cy-190)*S,(x+w)*S,(cy+190)*S],radius=w/2*S,fill=c); x+=w+g
    badge(d); return fin(t,l,m)
def v6(): # gradient tile (blue→purple) with white bars
    t,m=tile(((70,110,240),(130,60,200))); l=layer(); d=ImageDraw.Draw(l); bars(d,5,96,46,N/2-40,N/2-40,[220,400,540,360,200],[WH]*5); badge(d,R); return fin(t,l,m)
def v7(): # white, bars rounded top only (pillars), baseline aligned, colored
    t,m=tile(WH); l=layer(); d=ImageDraw.Draw(l); base=N/2+250-30
    w=110;g=44;x=N/2-(4*w+3*g)/2-40
    for h,c in zip([260,440,560,360],COL):
        d.rounded_rectangle([x*S,(base-h)*S,(x+w)*S,base*S],radius=w/2*S,fill=c); x+=w+g
    badge(d); return fin(t,l,m)
def v8(): # dark, circular ring + bars inside
    t,m=tile(((20,22,34),(20,22,34))); l=layer(); d=ImageDraw.Draw(l); cx=cy=N/2-30
    for i,c in enumerate(COL):
        d.arc([(cx-300)*S,(cy-300)*S,(cx+300)*S,(cy+300)*S],i*90+8,i*90+82,fill=c,width=34*S)
    bars(d,5,52,26,cx,cy,[110,200,290,180,100],[WH]*5); badge(d); return fin(t,l,m)
def v9(): # white, bars fading in height left→right, each color, with dot "recording"
    t,m=tile(WH); l=layer(); d=ImageDraw.Draw(l); bars(d,4,120,40,N/2-40,N/2-40,[520,400,290,180],COL); badge(d,R); return fin(t,l,m)
def v10(): # light-gray tile, 6 bars paired colors
    t,m=tile(((248,249,252),(226,229,238))); l=layer(); d=ImageDraw.Draw(l); bars(d,6,80,36,N/2-30,N/2-30,[200,380,520,440,300,170],[G,C,R,Y,G,C]); badge(d); return fin(t,l,m)

import shutil
ORANGE=(240,105,25,255); BLUE=(40,110,230,255); GREY=(110,110,120,255); AMBER=(245,170,20,255)
def glyph_base(v):
    l=layer(); d=ImageDraw.Draw(l); cx=cy=N/2-60
    if v==1: bars(d,4,150,70,cx,cy,[420,740,560,330],COL)
    elif v==2: bars(d,7,96,48,cx,cy,[260,480,700,540,640,400,240],COL)
    else:
        for i,c in enumerate(COL): d.arc([(cx-420)*S,(cy-420)*S,(cx+420)*S,(cy+420)*S],i*90+8,i*90+82,fill=c,width=70*S)
        bars(d,5,80,40,cx,cy,[170,300,430,270,150],[(90,90,100,255)]*5)
    return l
def sbadge(l,state):
    d=ImageDraw.Draw(l); r=270; cx=cy=N-r-10
    if state=="idle": return
    col={"recording":(230,50,50,255),"transcribing":(130,80,220,255),"paused":GREY,"error":AMBER,"attention":ORANGE}[state]
    d.ellipse([(cx-r)*S,(cy-r)*S,(cx+r)*S,(cy+r)*S],fill=col)
    fg=WH; w=34*S
    if state=="transcribing":
        d.ellipse([(cx-.5*r)*S,(cy-.5*r)*S,(cx+.5*r)*S,(cy+.5*r)*S],outline=fg,width=w); d.line([cx*S,cy*S,cx*S,(cy-.32*r)*S],fill=fg,width=w); d.line([cx*S,cy*S,(cx+.24*r)*S,(cy+.1*r)*S],fill=fg,width=w)
    elif state=="paused":
        for sx in(-.22,.22): d.rectangle([(cx+r*sx-.1*r)*S,(cy-.4*r)*S,(cx+r*sx+.1*r)*S,(cy+.4*r)*S],fill=fg)
    elif state=="error":
        d.rectangle([(cx-.08*r)*S,(cy-.5*r)*S,(cx+.08*r)*S,(cy+.1*r)*S],fill=fg); d.ellipse([(cx-.1*r)*S,(cy+.25*r)*S,(cx+.1*r)*S,(cy+.45*r)*S],fill=fg)
    elif state=="attention":  # padlock: permission missing
        d.rectangle([(cx-.4*r)*S,(cy-.05*r)*S,(cx+.4*r)*S,(cy+.5*r)*S],fill=fg)
        d.arc([(cx-.28*r)*S,(cy-.55*r)*S,(cx+.28*r)*S,(cy+.15*r)*S],180,360,fill=fg,width=int(.13*r*S)); d.rectangle([(cx-.28*r)*S,(cy-.22*r)*S,(cx-.15*r)*S,(cy)*S],fill=fg); d.rectangle([(cx+.15*r)*S,(cy-.22*r)*S,(cx+.28*r)*S,(cy)*S],fill=fg)
def mbicon(v,state,gray=False):
    l=glyph_base(v)
    if gray: l=l.convert("LA").convert("RGBA"); l.putalpha(l.getchannel("A").point(lambda x:int(x*.45)))
    sbadge(l,state); return l.resize((N,N),Image.LANCZOS)

def ring_layer(w, R, cx, cy, gap=7):
    l=layer(); d=ImageDraw.Draw(l)
    for i,c in enumerate(COL):
        d.arc([(cx-R)*S,(cy-R)*S,(cx+R)*S,(cy+R)*S],i*90+gap,i*90+90-gap,fill=c,width=int(w*S))
    return l
def app(w):
    t,m=tile(((20,22,34),(20,22,34))); cx=cy=N/2-75
    l=ring_layer(w,265+w*.15,cx,cy); badge(ImageDraw.Draw(l)); return fin(t,l,m)
def mb(w,state,gray=False):
    cx=cy=N/2-130; l=ring_layer(w*1.9,380,cx,cy,gap=9)
    if gray: l=l.convert("LA").convert("RGBA"); l.putalpha(l.getchannel("A").point(lambda x:int(x*.45)))
    sbadge(l,state); return l.resize((N,N),Image.LANCZOS)
BLUE=(40,110,230,255); GREY=(110,110,120,255); AMBER=(245,170,20,255)
def sbadge(l,state):
    d=ImageDraw.Draw(l); r=230; cx=cy=N-r-10
    if state=="idle": return
    col={"recording":(230,50,50,255),"transcribing":(130,80,220,255),"paused":GREY,"error":AMBER,"attention":ORANGE}[state]
    d.ellipse([(cx-r)*S,(cy-r)*S,(cx+r)*S,(cy+r)*S],fill=col)
    fg=WH; w=34*S
    if state=="transcribing":
        d.ellipse([(cx-.5*r)*S,(cy-.5*r)*S,(cx+.5*r)*S,(cy+.5*r)*S],outline=fg,width=w); d.line([cx*S,cy*S,cx*S,(cy-.32*r)*S],fill=fg,width=w); d.line([cx*S,cy*S,(cx+.24*r)*S,(cy+.1*r)*S],fill=fg,width=w)
    elif state=="paused":
        for sx in(-.22,.22): d.rectangle([(cx+r*sx-.1*r)*S,(cy-.4*r)*S,(cx+r*sx+.1*r)*S,(cy+.4*r)*S],fill=fg)
    elif state=="error":
        d.rectangle([(cx-.08*r)*S,(cy-.5*r)*S,(cx+.08*r)*S,(cy+.1*r)*S],fill=fg); d.ellipse([(cx-.1*r)*S,(cy+.25*r)*S,(cx+.1*r)*S,(cy+.45*r)*S],fill=fg)
    elif state=="attention":  # padlock: permission missing
        d.rectangle([(cx-.4*r)*S,(cy-.05*r)*S,(cx+.4*r)*S,(cy+.5*r)*S],fill=fg)
        d.arc([(cx-.28*r)*S,(cy-.55*r)*S,(cx+.28*r)*S,(cy+.15*r)*S],180,360,fill=fg,width=int(.13*r*S)); d.rectangle([(cx-.28*r)*S,(cy-.22*r)*S,(cx-.15*r)*S,(cy)*S],fill=fg); d.rectangle([(cx+.15*r)*S,(cy-.22*r)*S,(cx+.28*r)*S,(cy)*S],fill=fg)

