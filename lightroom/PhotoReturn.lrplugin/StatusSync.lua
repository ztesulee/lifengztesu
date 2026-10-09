local C=require 'Common'
local LrTasks=import 'LrTasks'
local LrApplication=import 'LrApplication'
local S={stopped=false}
function S.sync()
    if not C.prefs.token or C.prefs.token=='' then return end
    local catalogPath=C.catalog:getPath()
    local updates=C.request('/photo-updates',{catalog=catalogPath})
    local applied={};for id,value in pairs(C.prefs.appliedReturnUpdates or {}) do applied[id]=value end
    local ack={}
    if #updates>0 then
        C.catalog:withWriteAccessDo('同步返图状态',function()
            for _,update in ipairs(updates) do
                if applied[update.id] then ack[#ack+1]=update.id
                else
                    local photo=C.catalog:getPhotoByLocalId(tonumber(update.local_id))
                    if photo and photo:getRawMetadata('path')==update.source_path then
                        photo:setPropertyForPlugin(_PLUGIN,'returned','已返图')
                        applied[update.id]=true;ack[#ack+1]=update.id
                    end
                end
            end
        end,{timeout=2})
        C.prefs.appliedReturnUpdates=applied
        if #ack>0 then C.request('/ack-updates',{catalog=catalogPath,ids=ack}) end
    end
end
function S.start()
    if S.running then return end
    S.running=true;S.stopped=false
    LrTasks.startAsyncTask(function()
        while not S.stopped do
            LrTasks.pcall(S.sync)
            LrTasks.sleep(3)
        end
    end)
    LrTasks.startAsyncTask(function()
        while not S.stopped do
            C.catalog=LrApplication.activeCatalog()
            LrTasks.pcall(C.autoPair)
            -- Backend may be offline; keep the Library usable without repeated dialogs.
            if C.prefs.token and C.prefs.token~='' then
                LrTasks.pcall(function()
                    local command=C.request('/toolbar-command',{catalog=C.catalog:getPath(),running=C.toolbarRunningId or ''})
                    local scripts={selected='SendSelectedAction',folder='SendFolderAction',bind='BindFoldersAction',
                        bind_parent='BindParentAction',folder_status='FolderStatusAction',settings='TaskStatusAction'}
                    if scripts[command.action] then
                        C.toolbarCommandId=command.id
                        local ok,err=LrTasks.pcall(function()
                            local action=require(scripts[command.action])
                            assert(type(action)=='function','工具栏操作入口不是函数')
                            C.guarded(action)
                        end)
                        if not ok then
                            C.toolbarCommandId=nil
                            C.toolbarRunningId=nil
                            C.request('/toolbar-done',{id=command.id,ok=false,error=tostring(err)})
                        elseif C.toolbarCommandId==command.id then
                            C.toolbarCommandId=nil
                            C.request('/toolbar-done',{id=command.id,ok=false,error='菜单脚本没有启动，请重新载入插件后再试'})
                        end
                    end
                end)
            end
            LrTasks.sleep(3)
        end
        S.running=false
    end)
end
return S
