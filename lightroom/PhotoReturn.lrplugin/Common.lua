local C = {}
local LrApplication = import 'LrApplication'
local LrPathUtils = import 'LrPathUtils'
local LrPrefs = import 'LrPrefs'
local LrDialogs = import 'LrDialogs'
local LrFunctionContext = import 'LrFunctionContext'
local LrBinding = import 'LrBinding'
local LrView = import 'LrView'
local LrHttp = import 'LrHttp'
local LrFileUtils = import 'LrFileUtils'
local LrTasks = import 'LrTasks'
local Json = require 'Json'
C.prefs = LrPrefs.prefsForPlugin()
C.catalog = LrApplication.activeCatalog()
function C.folder(photo) return LrPathUtils.parent(photo:getRawMetadata('path')) end
function C.key(folder) return C.catalog:getPath() .. '|' .. folder end
function C.contact(folder)
    local contacts=C.prefs.contacts or {};local current=folder
    while current and current~='' do
        if contacts[C.key(current)] then return contacts[C.key(current)],current end
        local parent=LrPathUtils.parent(current)
        if parent==current then break end
        current=parent
    end
end
function C.photoContact(photo,metadataOnly)
    local function field(key)
        local value=photo.getPropertyForPlugin and photo:getPropertyForPlugin(_PLUGIN,key) or ''
        return tostring(value or ''):match('^%s*(.-)%s*$')
    end
    local qq,wechat,name,channel=field('qq'),field('wechat'),field('person'),field('channel')
    if metadataOnly and qq=='' and wechat=='' then error('请先在选中素材的元数据中填写 QQ 或微信号：'..photo:getRawMetadata('path')) end
    local inherited=C.contact(C.folder(photo))
    if qq=='' and wechat=='' and not inherited then return nil end
    local explicit=qq~='' or wechat~='' or name~='' or channel~=''
    if qq=='' and wechat=='' then qq=inherited.qq or '';wechat=inherited.wechat or '' end
    if qq~='' and not qq:match('^%d+$') then error('照片元数据 QQ 号码必须为纯数字：'..photo:getRawMetadata('path')) end
    local channels={QQ='qq',qq='qq',['微信']='wechat',wechat='wechat',['自动']='default',default='default'}
    if channel~='' and not channels[channel] then error('元数据发送渠道请填写 QQ、微信或自动。') end
    return {name=name~='' and name or (inherited and inherited.name or LrPathUtils.leafName(C.folder(photo))),
        qq=qq,wechat=wechat,channel=channels[channel] or (inherited and inherited.channel or 'default')},explicit
end
function C.bind(folder)
    local saved=C.contact(folder) or {}
    return LrFunctionContext.callWithContext('bind folder',function(context)
        local p=LrBinding.makePropertyTable(context)
        p.name=saved.name or LrPathUtils.leafName(folder);p.qq=saved.qq or '';p.wechat=saved.wechat or '';p.channel=saved.channel or 'default'
        local f=LrView.osFactory()
        local contents=f:column { bind_to_object=p, spacing=8,
            f:static_text { title=folder, width_in_chars=65, wrap=true },
            f:row { f:static_text { title='人物名称',width_in_chars=12 }, f:edit_field { value=LrView.bind('name'),width_in_chars=36 } },
            f:row { f:static_text { title='QQ 号码',width_in_chars=12 }, f:edit_field { value=LrView.bind('qq'),width_in_chars=36 } },
            f:row { f:static_text { title='微信号 / 搜索名',width_in_chars=12 }, f:edit_field { value=LrView.bind('wechat'),width_in_chars=36 } },
            f:popup_menu { value=LrView.bind('channel'), items={{title='自动（只填一个账号时使用该账号）',value='default'},{title='QQ',value='qq'},{title='微信',value='wechat'}} },
            f:static_text { title='同时填写两个账号时，自动默认使用 QQ。人物名称仅用于返图，不会重命名磁盘文件夹。' },
        }
        if LrDialogs.presentModalDialog { title='绑定人物文件夹',contents=contents,actionVerb='保存账号' }~='ok' then return nil end
        p.name=p.name:match('^%s*(.-)%s*$');p.qq=p.qq:match('^%s*(.-)%s*$');p.wechat=p.wechat:match('^%s*(.-)%s*$')
        if p.name=='' or (p.qq=='' and p.wechat=='') or (p.qq~='' and not p.qq:match('^%d+$')) then
            LrDialogs.message('无法保存','人物名称和至少一个账号不能为空；QQ 必须为纯数字。','critical');return nil
        end
        local all={};for key,value in pairs(C.prefs.contacts or {}) do all[key]=value end
        local result={name=p.name,qq=p.qq,wechat=p.wechat,channel=p.channel}
        all[C.key(folder)]=result;C.prefs.contacts=all;return result
    end)
end
function C.autoPair()
    if C.toolbarRunningId or not _PLUGIN or not _PLUGIN.path then return false end
    local root=LrPathUtils.parent(LrPathUtils.parent(_PLUGIN.path))
    local config=LrPathUtils.child(LrPathUtils.child(root,'.lightroom-state'),'connection.json')
    if not LrFileUtils.exists(config) then return false end
    local settings=Json.decode(LrFileUtils.readFile(config))
    local token=settings.token
    if type(token)~='string' or #token<32 then return false end
    if C.prefs.token==token then return true end
    local body=LrHttp.get('http://127.0.0.1:17861/health',{{field='X-Return-Token',value=token}},2)
    if not body then return false end
    local health=Json.decode(body)
    if health.service~='photo-return-lightroom' then return false end
    C.prefs.token=token
    return true
end
function C.request(path,value)
    local token=C.prefs.token or ''
    if token=='' then error('等待自动连接；请先启动本机返图后台并启用 Lightroom 插件。') end
    local headers={{field='X-Return-Token',value=token},{field='Content-Type',value='application/json'}}
    local body,response
    if value then body,response=LrHttp.post('http://127.0.0.1:17861'..path,Json.encode(value),headers,'POST',path=='/submit' and 300 or 10)
    else body,response=LrHttp.get('http://127.0.0.1:17861'..path,headers,10) end
    if not body then error('无法连接返图后台，请先运行“启动Lightroom返图后台.bat”。') end
    local result=Json.decode(body)
    if result.error then error(result.error) end
    return result
end
function C.guarded(action)
    local commandId=C.toolbarCommandId
    C.toolbarCommandId=nil
    if commandId then C.toolbarRunningId=commandId end
    LrTasks.startAsyncTask(function()
        local ok,err=LrTasks.pcall(action)
        if not ok and C.exporting then
            LrTasks.pcall(function()C.exportProgress(C.exportDone or 0,C.exportTotal or 0,'error','导出失败或取消：'..tostring(err))end)
        end
        if commandId then C.toolbarRunningId=nil end
        if commandId then LrTasks.pcall(function()C.request('/toolbar-done',{id=commandId,ok=ok,error=not ok and tostring(err) or ''})end) end
        if not ok then LrDialogs.message('返图操作失败',tostring(err),'critical') end
    end)
end
function C.exportProgress(done,total,state,message)
    C.exporting=state=='running';C.exportDone=done;C.exportTotal=total
    C.request('/export-progress',{done=done,total=total,state=state or 'running',
        message=message or ('正在导出图片：'..done..' / '..total)})
end
function C.send(rendered,confirmed,archiveDirectory,exportDirectory)
    local byFolder,batches={},{}
    for _,entry in ipairs(rendered) do
        local folder=C.folder(entry.photo)
        local contact,explicit
        if entry.contact then contact=entry.contact;explicit=true
        else contact,explicit=C.photoContact(entry.photo) end
        contact=contact or C.bind(folder)
        if not contact then error('已取消，没有提交发送任务。') end
        local useQQ=contact.qq~='' and (contact.wechat=='' or contact.channel~='wechat')
        local personKey=Json.encode({contact.name,useQQ and 'qq' or 'wechat',useQQ and contact.qq or contact.wechat})
        local key=personKey
        if not byFolder[key] then
            local b={folder=C.key(folder),folders={},name=contact.name,contacts={qq=contact.qq,wechat=contact.wechat},channel=contact.channel,files={},photos={}}
            if archiveDirectory then b.archive_directory=archiveDirectory end
            if exportDirectory then b.export_directory=exportDirectory end
            if explicit then b.person_key=personKey end
            byFolder[key]=b;batches[#batches+1]=b
        end
        local sourceKey=C.key(folder);local found=false
        for _,source in ipairs(byFolder[key].folders) do if source==sourceKey then found=true end end
        if not found then byFolder[key].folders[#byFolder[key].folders+1]=sourceKey end
        local files=byFolder[key].files;files[#files+1]=entry.path
        local photos=byFolder[key].photos
        photos[#photos+1]={catalog=C.catalog:getPath(),local_id=tostring(entry.photo.localIdentifier),
            source_path=entry.photo:getRawMetadata('path'),export_path=entry.path}
    end
    local lines={}
    for _,b in ipairs(batches) do
        local qq=b.contacts.qq~='';local wx=b.contacts.wechat~=''
        local useQQ=qq and (not wx or b.channel~='wechat')
        lines[#lines+1]=b.name..' · '..(useQQ and 'QQ：'..b.contacts.qq or '微信：'..b.contacts.wechat)..' · '..#b.files..' 项'
    end
    if #batches==0 then error('没有成功导出的素材，未发送。') end
    if not confirmed and LrDialogs.confirm('确认发送以下素材',table.concat(lines,'\n')..'\n\n发送期间请保持 QQ / 微信登录，勿操作鼠标键盘。','确认发送','取消')~='ok' then return end
    local result=C.request('/submit',{batches=batches})
    LrDialogs.message('已加入返图队列','已创建 / 复用 '..#result.tasks..' 个任务。通过“连接设置与任务状态”查看结果。')
end
return C
