"""Build an allowlisted, portable Lightroom delivery release without user data."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parent
VERSION='0.1.29'

def main():
    name='PhotoReturn-Lightroom-'+VERSION+'-Windows-x64-'+time.strftime('%Y%m%d-%H%M%S')
    output=ROOT/'release'/name
    work=ROOT/'build'/name
    output.mkdir(parents=True)
    work.mkdir(parents=True)
    env=os.environ.copy()
    env['PYTHONPATH']=os.pathsep.join(str(ROOT/p) for p in ('.packaging-tools','.deps','.deps/win32','.deps/win32/lib','.deps/pythonwin','.deps/pywin32_system32'))
    env['PYTHONUTF8']='1'
    env['PATH']=str(ROOT/'.deps'/'pywin32_system32')+os.pathsep+env.get('PATH','')
    args=[sys.executable,'-m','PyInstaller','--noconfirm','--onedir','--windowed',
          '--name','PhotoReturnBackend','--distpath',str(work/'dist'),'--workpath',str(work/'cache'),
          '--specpath',str(work),'--paths',str(ROOT),
          '--add-data',str(ROOT/'windows_ocr.ps1')+';.',
          '--add-data',str(ROOT/'assets'/'wechat-file-icon.png')+';assets',
          '--add-binary',str(ROOT/'.deps'/'pywin32_system32'/'pythoncom39.dll')+';.',
          '--add-binary',str(ROOT/'.deps'/'pywin32_system32'/'pywintypes39.dll')+';.',
          '--collect-submodules','pywinauto','--collect-submodules','comtypes',
          '--hidden-import','win32clipboard','--hidden-import','win32gui','--hidden-import','win32process',
          '--hidden-import','pythoncom','--hidden-import','pywintypes',
          '--hidden-import','win32ui',
          '--hidden-import','qq_sender','--hidden-import','wechat_sender',
          '--hidden-import','wechat_visual','--hidden-import','lightroom_task_window']
    for module in ('torch','onnx','onnxruntime','rawpy','imageio_ffmpeg','matplotlib','scipy','pandas','IPython','pytest'):
        args.extend(['--exclude-module',module])
    args.append(str(ROOT/'lightroom_bridge.py'))
    subprocess.run(args,cwd=ROOT,env=env,check=True)
    shutil.copytree(work/'dist'/'PhotoReturnBackend',output,dirs_exist_ok=True)
    plugin=output/'lightroom'/'PhotoReturn.lrplugin'
    plugin.mkdir(parents=True)
    for source in (ROOT/'lightroom'/'PhotoReturn.lrplugin').glob('*.lua'):
        shutil.copy2(source,plugin/source.name)
    launcher='@echo off\r\ncd /d "%~dp0"\r\nset PHOTO_RETURN_DEBUG=1\r\nstart "" "PhotoReturnBackend.exe"\r\n'
    (output/'启动返图后台.bat').write_bytes(launcher.encode('ascii'))
    instructions='''# 摄影返图 Lightroom 插件 0.1.29（Windows x64）

## 安装
1. 将 ZIP 完整解压到可写目录，例如 D:\\摄影返图。不要直接在压缩包内运行，也不要只复制 EXE；_internal 和 lightroom 文件夹必须保留。
2. 双击“启动返图后台.bat”或 PhotoReturnBackend.exe，无须安装 Python。
3. Lightroom Classic：文件 → 增效工具管理器 → 添加，选择 lightroom\\PhotoReturn.lrplugin 文件夹，确认插件启用。
4. 插件会读取发布目录中的本机连接配置自动配对。若暂未连接，重新载入插件并确认后台运行。同一电脑只能运行一个返图后台；先关闭原开发版后台。
5. 在 Lightroom 右侧元数据中选择“摄影返图 · QQ / 微信”，填写人物名称、QQ/微信账号及发送渠道。选中照片，用悬浮工具栏“发送已选中素材”设置导出参数并确认收件人。

## 发送和任务
- 照片由 Lightroom 自身导出 JPEG，视频复制原文件。QQ/微信客户端须先登录；需要保持桌面解锁。自动发送会操作客户端，期间不要操作鼠标键盘。
- 微信同一人物任务 1–8 张纯照片直接发送，9 张及以上或包含视频时发送 ZIP。QQ 使用当前闪传流程；逐个账号发送，下一任务可预打包。
- 导出素材按“人物名称_实际账号”保存。同名且账号相同的目录会清空，仅保留本次素材（包括独立 ZIP 人物目录）；仍被未完成任务占用的目录会禁止覆盖。
- 任务窗可以核对/继续、取消、终止和删除任务记录，并编辑“留言模板”。已提交客户端不代表对方接收或下载完成。
- 重启后旧任务会暂停，需要单独继续；新任务不会被旧暂停任务阻塞。恢复已取消任务时，会检查素材是否仍存在。

## 数据与分享
- 首次启动在本包目录生成 .lightroom-state，保存本机连接口令、联系人、任务和留言模板。logs 保存诊断记录。
- 给别人分享原始 ZIP。不要把自己使用后的 .lightroom-state、logs、导出照片或个人 Lightroom 目录文件一起打包。
- 升级前关闭后台。保留自己的 .lightroom-state 可延续任务；旧导出素材也需保留。移动发布包后，在 Lightroom 重新添加新路径下插件。

## 兼容说明
- 目标系统为 Windows 10/11 x64 与 Lightroom Classic；不适用于云版 Lightroom、macOS。开发使用 Lightroom Classic 15.5.1，其他版本尚未完整验证。
- 已进行 EXE 依赖和资源自检及代码测试；QQ/微信界面因版本不同可能需要适配。第一次先用自己的联系人和少量素材验证。
- 包内不附带 QQ、微信、Lightroom 或用户数据。不含代码签名证书；启动异常时检查解压是否完整、目录是否可写、是否有其他后台占用端口。
'''
    (output/'安装与使用说明.md').write_text(instructions,encoding='utf-8-sig')
    # Run only the dependency/state self-test: no GUI, login, or message sending.
    report=work/'self-test.json'
    subprocess.run([str(output/'PhotoReturnBackend.exe'),'--self-test',str(report)],check=True,timeout=90)
    data=json.loads(report.read_text(encoding='utf-8'))
    if not data.get('ok'):raise RuntimeError('Frozen executable self-test failed')
    forbidden={'.lightroom-state','logs','project.json','connection.json','摄影选片.toml','.qq-contact-aliases.json'}
    if any(p.name in forbidden for p in output.rglob('*')):raise RuntimeError('Private runtime file detected')
    manifest={'version':VERSION,'platform':'Windows x64','files':[
        {'path':str(p.relative_to(output)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in output.rglob('*') if p.is_file()]}
    (output/'文件校验清单.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    archive=shutil.make_archive(str(output),'zip',root_dir=output.parent,base_dir=output.name)
    print('RELEASE='+archive,flush=True)
    print('BYTES='+str(Path(archive).stat().st_size),flush=True)

if __name__=='__main__':main()
