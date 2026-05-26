#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, hashlib, json, shutil
from dataclasses import dataclass, asdict
from pathlib import Path
import imagehash, numpy as np
from PIL import Image, ImageOps

IMG_EXT=(".jpg",".jpeg",".png",".webp",".gif",".bmp",".avif",".tif",".tiff")
@dataclass
class D: local_path:str; sha256:str=""; source_url:str=""; final_url:str=""; found_on_page:str=""; page_title:str=""; page_dates:str=""; width:int=0; height:int=0
@dataclass
class R:
    rank:int; combined_score_percent:float; full_image_score_percent:float; face_match_percent:str; face_match_reason:str
    hash_score_percent:float; phash_percent:float; dhash_percent:float; color_percent:float; clip_cosine:str
    face_count:int; body_count:int; target_face_count:int; target_body_count:int; width:int; height:int; sha256:str
    local_path:str; copied_path:str; source_url:str; final_url:str; found_on_page:str; page_title:str; page_dates:str; face_crops_folder:str; body_crops_folder:str

def mkdir(p): p=Path(p); p.mkdir(parents=True,exist_ok=True); return p
def sha_file(p):
    h=hashlib.sha256();
    with open(p,'rb') as f:
        for b in iter(lambda:f.read(1<<20),b''): h.update(b)
    return h.hexdigest()
def load(p):
    im=Image.open(p); im=ImageOps.exif_transpose(im); im.load(); return im.convert('RGB')
def fp(im): return (imagehash.phash(im,hash_size=16), imagehash.dhash(im,hash_size=16), imagehash.colorhash(im,binbits=3))
def sim(a,b): return max(0.0,1.0-((a-b)/a.hash.size))
def score(ta,ca):
    ph,dh,ch=sim(ta[0],ca[0]),sim(ta[1],ca[1]),sim(ta[2],ca[2]); return .62*ph+.25*dh+.13*ch,ph,dh,ch

class Clip:
    def __init__(self,on):
        self.model=None
        if on:
            try:
                from sentence_transformers import SentenceTransformer
                print('[clip] loading clip-ViT-B-32'); self.model=SentenceTransformer('clip-ViT-B-32')
            except Exception as e: print('[warn] CLIP disabled:',e)
    def enc(self,im):
        if not self.model: return None
        return self.model.encode([im],convert_to_numpy=True,normalize_embeddings=True)[0].astype(np.float32)
    def cos(self,a,b): return None if a is None or b is None else float(np.dot(a,b))

class Det:
    def __init__(self,face=True,body=False,prefer_mtcnn=True):
        self.cv2=None; self.fd=None; self.pd=None; self.bd=None; self.mtcnn=None; self.detector='none'
        if face and prefer_mtcnn:
            try:
                from mtcnn import MTCNN
                self.mtcnn=MTCNN(); self.detector='mtcnn'
                print('[face-detector] MTCNN enabled')
            except Exception as e:
                print('[warn] MTCNN unavailable; using OpenCV fallback:',e)
        if face or body:
            try: import cv2; self.cv2=cv2
            except Exception: print('[warn] opencv unavailable'); return
        if face and self.cv2:
            try:
                self.fd=self.cv2.CascadeClassifier(self.cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
                self.pd=self.cv2.CascadeClassifier(self.cv2.data.haarcascades+'haarcascade_profileface.xml')
                if self.fd.empty(): self.fd=None
                if self.pd.empty(): self.pd=None
                self.detector = 'mtcnn+opencv' if self.mtcnn else 'opencv_haar'
            except Exception: self.fd=self.pd=None
        if body and self.cv2:
            try:
                hog=self.cv2.HOGDescriptor(); hog.setSVMDetector(self.cv2.HOGDescriptor_getDefaultPeopleDetector()); self.bd=hog
            except Exception: self.bd=None
    def faces_mtcnn(self,im):
        if self.mtcnn is None: return []
        try:
            arr=np.array(im)
            dets=self.mtcnn.detect_faces(arr)
            boxes=[]
            for d in dets:
                if float(d.get('confidence',0)) < 0.80: continue
                x,y,w,h=d.get('box',[0,0,0,0]); x=max(0,int(x)); y=max(0,int(y)); w=int(w); h=int(h)
                if w>=20 and h>=20: boxes.append((x,y,w,h))
            return dedupe(boxes)
        except Exception: return []
    def faces_cv(self,im):
        if not self.cv2: return []
        try:
            arr=np.ascontiguousarray(np.array(im).astype('uint8'))
            gray=self.cv2.cvtColor(arr,self.cv2.COLOR_RGB2GRAY); gray=self.cv2.equalizeHist(gray)
        except Exception: return []
        boxes=[]
        for d in (self.fd,self.pd):
            if d is None: continue
            try:
                for x,y,w,h in d.detectMultiScale(gray,scaleFactor=1.05,minNeighbors=3,minSize=(22,22)): boxes.append((int(x),int(y),int(w),int(h)))
            except Exception: pass
        return dedupe(boxes)
    def faces(self,im):
        boxes=self.faces_mtcnn(im)
        return boxes if boxes else self.faces_cv(im)
    def bodies(self,im):
        if not self.cv2 or self.bd is None: return []
        try:
            arr=np.ascontiguousarray(np.array(im).astype('uint8')); h,w=arr.shape[:2]
            if h<96 or w<48: return []
            s=min(1.0,900/max(h,w)); small=arr
            if s<1: small=np.ascontiguousarray(self.cv2.resize(arr,(max(1,int(w*s)),max(1,int(h*s)))).astype('uint8'))
            try: boxes,_=self.bd.detectMultiScale(small,winStride=(8,8),padding=(16,16),scale=1.05)
            except Exception: return []
            out=[]
            for x,y,bw,bh in boxes:
                if s<1: x,y,bw,bh=int(x/s),int(y/s),int(bw/s),int(bh/s)
                out.append((int(x),int(y),int(bw),int(bh)))
            return dedupe(out)
        except Exception: return []

def dedupe(boxes):
    out=[]
    for x,y,w,h in boxes:
        keep=True
        for bx,by,bw,bh in out:
            ix=max(0,min(x+w,bx+bw)-max(x,bx)); iy=max(0,min(y+h,by+bh)-max(y,by))
            if (ix*iy)/max(1,min(w*h,bw*bh))>.45: keep=False; break
        if keep: out.append((x,y,w,h))
    return out
def center_box(im):
    w,h=im.size; side=max(32,int(min(w,h)*.55)); x=max(0,(w-side)//2); y=max(0,int(h*.18));
    if y+side>h: y=max(0,(h-side)//2)
    return (x,y,min(side,w-x),min(side,h-y))
def crops(im,boxes,pad=.34):
    res=[]; iw,ih=im.size
    for x,y,w,h in boxes:
        p=int(max(w,h)*pad); l=max(0,x-p); t=max(0,y-p); r=min(iw,x+w+p); b=min(ih,y+h+p)
        if r>l and b>t: res.append(im.crop((l,t,r,b)).convert('RGB'))
    return res
def save_crops(im,boxes,folder,prefix):
    if not boxes: return ''
    folder=mkdir(folder)
    for i,c in enumerate(crops(im,boxes)[:20],1): c.save(folder/f'{prefix}_{i:02d}.jpg',quality=92)
    return str(folder)
def best_face(thashes, ccrops):
    if not thashes or not ccrops: return None
    best=0
    for c in ccrops:
        try: cf=fp(c)
        except Exception: continue
        for th in thashes:
            sc,_,_,_=score(th,cf); best=max(best,sc)
    return best if best>0 else None

def read_meta(p):
    out=[]; base=Path(p).parent
    with open(p,newline='',encoding='utf-8') as f:
        for row in csv.DictReader(f):
            lp=row.get('local_path',''); pp=Path(lp)
            if not pp.exists(): pp=base/lp
            if not pp.exists(): continue
            out.append(D(str(pp),row.get('sha256',''),row.get('source_url',''),row.get('final_url',''),row.get('found_on_page',''),row.get('page_title',''),row.get('page_dates',''),int(row.get('width') or 0),int(row.get('height') or 0)))
    return out
def read_folder(folder): return [D(str(p),sha_file(p)) for p in Path(folder).rglob('*') if p.is_file() and p.suffix.lower() in IMG_EXT]
def write(rows,p):
    with open(p,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(R.__dataclass_fields__.keys())); w.writeheader(); [w.writerow(asdict(r)) for r in rows]

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('target_image')
    g=ap.add_mutually_exclusive_group(required=True); g.add_argument('--metadata'); g.add_argument('--folder')
    ap.add_argument('--output',default='local_match_results'); ap.add_argument('--top',type=int,default=100); ap.add_argument('--copy-top',type=int,default=50)
    ap.add_argument('--clip',action='store_true'); ap.add_argument('--no-face-detect',action='store_true'); ap.add_argument('--no-face-match',action='store_true')
    ap.add_argument('--target-face-image',default=''); ap.add_argument('--face-fallback-center',action='store_true'); ap.add_argument('--candidate-face-fallback-center',action='store_true')
    ap.add_argument('--disable-mtcnn',action='store_true'); ap.add_argument('--body-detect',action='store_true'); ap.add_argument('--min-width',type=int,default=40); ap.add_argument('--min-height',type=int,default=40); ap.add_argument('--limit',type=int,default=0)
    a=ap.parse_args(); out=mkdir(a.output); topdir=mkdir(out/'top_matches'); facesdir=mkdir(out/'face_crops'); bodiesdir=mkdir(out/'body_crops'); tfdir=mkdir(out/'target_face_crops')
    target=load(a.target_image); th=fp(target); clip=Clip(a.clip); te=clip.enc(target) if clip.model else None
    det=Det(not a.no_face_detect,a.body_detect,prefer_mtcnn=not a.disable_mtcnn); tfaces=det.faces(target); tbodies=det.bodies(target); save_crops(target,tbodies,out/'target_body_crops','target_body')
    tcrops=[]; reason=''
    if a.target_face_image:
        m=load(a.target_face_image); tcrops=[m]; m.save(tfdir/'target_manual_face_01.jpg',quality=92); reason='manual target face image used'
    elif tfaces:
        tcrops=crops(target,tfaces); save_crops(target,tfaces,tfdir,'target_face'); reason='target face detected'
    elif a.face_fallback_center:
        box=center_box(target); tcrops=crops(target,[box]); save_crops(target,[box],tfdir,'target_center_fallback'); reason='center fallback target crop used'
    else: reason='no target face detected; use manual face crop or face fallback'
    thashes=[fp(c) for c in tcrops] if (tcrops and not a.no_face_match) else []
    data=read_meta(a.metadata) if a.metadata else read_folder(a.folder)
    if a.limit: data=data[:a.limit]
    print(f'[dataset] images={len(data)}'); print(f'[target] faces={len(tfaces)} bodies={len(tbodies)}'); print(f'[face-match] {reason}'); print('[face-match] enabled' if thashes else '[face-match] disabled/no usable target face crop')
    raw=[]; seen=set(); cand_face_detected=0; cand_face_scored=0
    for i,item in enumerate(data,1):
        if i==1 or i%100==0 or i==len(data): print(f'[compare] {i}/{len(data)} valid={len(raw)}')
        p=Path(item.local_path)
        try: im=load(p)
        except Exception: continue
        w,h=im.size
        if w<a.min_width or h<a.min_height: continue
        sha=item.sha256 or sha_file(p)
        if sha in seen: continue
        seen.add(sha)
        try: hs,ph,dh,ch=score(th,fp(im))
        except Exception: continue
        cscore=None
        if clip.model and te is not None:
            try: cscore=clip.cos(te,clip.enc(im))
            except Exception: pass
        full=hs if cscore is None else .55*hs+.45*max(0,min(1,(cscore+1)/2))
        fs=det.faces(im); bs=det.bodies(im)
        if fs: cand_face_detected+=1
        cc=crops(im,fs)
        if not cc and a.candidate_face_fallback_center: cc=crops(im,[center_box(im)])
        fscore=None; freason=reason
        if a.no_face_match: freason='face matching disabled'
        elif not thashes: freason=reason
        elif not cc: freason='no candidate face detected'
        else:
            fscore=best_face(thashes,cc); freason='face crop compared' if fscore is not None else 'face crop comparison failed'
            if fscore is not None: cand_face_scored+=1
        comb=.50*full+.50*fscore if fscore is not None else full
        raw.append(dict(comb=comb,full=full,hs=hs,ph=ph,dh=dh,ch=ch,clip=cscore,face=fscore,freason=freason,item=item,im=im,fs=fs,bs=bs,sha=sha,w=w,h=h))
    raw.sort(key=lambda x:x['comb'],reverse=True)
    rows=[]
    for rank,r in enumerate(raw,1):
        item=r['item']; copied=''; ff=''; bf=''
        if rank<=a.copy_top:
            suf=Path(item.local_path).suffix.lower(); suf=suf if suf in IMG_EXT else '.jpg'; cp=topdir/f'{rank:03d}_score_{r["comb"]:.3f}_{r["sha"][:12]}{suf}'
            shutil.copy2(item.local_path,cp); copied=str(cp); ff=save_crops(r['im'],r['fs'],facesdir/f'rank_{rank:03d}','face'); bf=save_crops(r['im'],r['bs'],bodiesdir/f'rank_{rank:03d}','body')
        rows.append(R(rank,round(r['comb']*100,3),round(r['full']*100,3),'' if r['face'] is None else str(round(r['face']*100,3)),r['freason'],round(r['hs']*100,3),round(r['ph']*100,3),round(r['dh']*100,3),round(r['ch']*100,3),'' if r['clip'] is None else str(round(r['clip'],6)),len(r['fs']),len(r['bs']),len(tfaces),len(tbodies),r['w'],r['h'],r['sha'],item.local_path,copied,item.source_url,item.final_url,item.found_on_page,item.page_title,item.page_dates,ff,bf))
    write(rows,out/'matches.csv')
    manifest=dict(detector_used=getattr(det,'detector','unknown'), target_image=a.target_image,target_face_image=a.target_face_image,dataset_count=len(data),valid_compared=len(rows),face_crop_similarity_enabled=bool(thashes),face_match_reason=reason,candidate_images_with_detected_faces=cand_face_detected,candidate_images_with_face_score=cand_face_scored,notes=['Scores are investigative leads for human review only, not biometric identity proof.','Blank face_match_percent means check face_match_reason in matches.csv.'])
    (out/'match_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print('\nTop matches:')
    for rec in rows[:a.top]: print(f'{rec.rank:>3}. combined={rec.combined_score_percent:6.2f}% full={rec.full_image_score_percent:6.2f}% face={rec.face_match_percent or "N/A"} reason={rec.face_match_reason}\n     file : {rec.local_path}\n     page : {rec.found_on_page}')
    print('\nSaved:'); print(out/'matches.csv'); print(topdir); print(facesdir); print(tfdir); print('[face-summary]',reason,'candidate_faces',cand_face_detected,'face_scores',cand_face_scored)
if __name__=='__main__': raise SystemExit(main())
