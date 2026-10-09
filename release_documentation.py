"""Create the shareable installation guide and third-party notices."""
import hashlib
import json
import shutil
import sys
from pathlib import Path
from xml.sax.saxutils import escape

def finish(folder):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer
    from reportlab.lib.pagesizes import A4
    pdfmetrics.registerFont(TTFont('GuideChinese','C:/Windows/Fonts/simsun.ttc',subfontIndex=0))
    style=ParagraphStyle('body',fontName='GuideChinese',fontSize=10,leading=16,wordWrap='CJK',spaceAfter=6)
    title=ParagraphStyle('title',parent=style,fontSize=18,leading=24,textColor=colors.HexColor('#173d59'),spaceAfter=15)
    heading=ParagraphStyle('heading',parent=style,fontSize=13,leading=20,spaceBefore=10,spaceAfter=7)
    story=[]
    for line in (folder/'安装与使用说明.md').read_text(encoding='utf-8-sig').splitlines():
        if not line:continue
        chosen=title if line.startswith('# ') else heading if line.startswith('## ') else style
        text=line.lstrip('# ').replace('–','-')
        story.append(Paragraph(escape(text),chosen))
    pdf=folder/'安装与使用说明.pdf'
    def footer(canvas,doc):
        canvas.setFont('GuideChinese',9)
        canvas.drawString(42,24,'摄影返图 Lightroom 插件 0.1.29')
        canvas.drawRightString(A4[0]-42,24,str(doc.page))
    SimpleDocTemplate(str(pdf),pagesize=A4,rightMargin=42,leftMargin=42,topMargin=38,bottomMargin=42,
                      title='摄影返图插件安装与使用说明').build(story,onFirstPage=footer,onLaterPages=footer)
    root=Path(__file__).resolve().parent
    notices=folder/'第三方许可';notices.mkdir(exist_ok=True)
    roots=[root/'.deps',Path('K:/Python-3.9.25/Lib/site-packages')]
    names=('numpy','pillow','opencv','pywin32','pywinauto','comtypes','six')
    for base in roots:
        for dist in base.glob('*.dist-info'):
            if not any(dist.name.casefold().startswith(n) for n in names):continue
            for source in dist.rglob('*'):
                if source.is_file() and ('license' in source.name.casefold() or 'copying' in source.name.casefold() or source.name=='METADATA'):
                    target=notices/dist.name/source.relative_to(dist)
                    target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    shutil.copy2(Path('K:/Python-3.9.25/LICENSE.txt'),notices/'Python-LICENSE.txt')
    manifest={'version':'0.1.29','platform':'Windows x64','files':[
        {'path':str(p.relative_to(folder)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in folder.rglob('*') if p.is_file() and p.name!='文件校验清单.json']}
    (folder/'文件校验清单.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return pdf

if __name__=='__main__':
    finish(Path(sys.argv[1]).resolve())
