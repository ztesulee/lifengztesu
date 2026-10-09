local C=require 'Common'
local LrDialogs=import 'LrDialogs'
local LrFunctionContext=import 'LrFunctionContext'
local LrBinding=import 'LrBinding'
local LrView=import 'LrView'
return function()
    local LrTasks=import 'LrTasks'
    LrTasks.pcall(C.autoPair)
    LrFunctionContext.callWithContext('connection settings',function(context)
        local p=LrBinding.makePropertyTable(context);p.token=C.prefs.token or ''
        local f=LrView.osFactory()
        if LrDialogs.presentModalDialog {title='返图后台连接',actionVerb='保存并查看任务',contents=f:column {
            bind_to_object=p,spacing=8,
            f:static_text {title='插件会自动连接本机返图后台。下方口令仅用于手动连接备用。'},
            f:edit_field {value=LrView.bind('token'),width_in_chars=60},
            f:static_text {title='后台地址固定为本机 127.0.0.1:17861；口令保存在 Lightroom 插件设置中。'},
        }}~='ok' then return end
        C.prefs.token=p.token:match('^%s*(.-)%s*$')
        local tasks=C.request('/tasks');local lines={}
        local states={queued='等待发送',waiting='重启后暂停',sending='正在发送',submitted='已提交客户端',error='失败，队列暂停',needs_review='结果待核对，队列暂停',cancelled='已取消'}
        for _,task in ipairs(tasks) do lines[#lines+1]=task.name..' · '..task.count..' 项 · '..(states[task.status] or task.status)..'\n'..task.id..' '..(task.error or '') end
        LrDialogs.message('最近返图任务',#lines>0 and table.concat(lines,'\n\n') or '暂无任务。')
        local paused={}
        for _,task in ipairs(tasks) do
            if task.status=='error' or task.status=='waiting' or task.status=='needs_review' then paused[#paused+1]={title=task.name..' · '..task.id,value=task.id} end
        end
        if #paused>0 then
            p.task=paused[1].value;p.resolution='skip'
            if LrDialogs.presentModalDialog {title='核对并继续暂停的任务',actionVerb='继续选中任务',contents=f:column {
                bind_to_object=p,spacing=8,
                f:popup_menu {value=LrView.bind('task'),items=paused},
                f:static_text {title='请先检查 QQ / 微信。只有结果不确定的任务需要以下选择；重发可能产生重复附件。'},
                f:popup_menu {value=LrView.bind('resolution'),items={{title='已发送：跳过不确定批次',value='skip'},{title='未发送：重新发送不确定批次',value='retry'},{title='取消此任务（不会撤回客户端附件）',value='cancel'}}},
            }}=='ok' then
                C.request('/resume',{id=p.task,resolution=p.resolution})
                LrDialogs.message('已处理',p.resolution=='cancel' and '任务已取消。' or '任务已重新进入队列。')
            end
        end
    end)
end
