local C=require 'Common'
local LrDialogs=import 'LrDialogs'
return function()
    local path=(require 'FolderTools').choose('查看人物与拍摄子文件夹状态')
    if not path then return end
    local folder=C.catalog:getFolderByPath(path)
    if not folder then error('该父文件夹不在 Lightroom 目录文件中。') end
    local paths={}
    local function collect(item)
        paths[#paths+1]=C.key(item:getPath())
        for _,child in ipairs(item:getChildren()) do collect(child) end
    end
    collect(folder)
    local states=C.request('/folder-status',{folders=paths});local lines={}
    local labels={submitted='已提交返图',queued='等待返图',sending='正在返图',waiting='暂停待继续',error='失败待处理',needs_review='待核对',cancelled='任务已取消'}
    local function visit(item,depth)
        local record=states[C.key(item:getPath())]
        local contact,from=C.contact(item:getPath())
        local status=record and (labels[record.status] or record.status) or '无返图记录'
        lines[#lines+1]=string.rep('  ',depth)..item:getName()..'：'..status..
            (record and (' · 最近任务 '..record.count..' 项 · '..record.created) or '')..
            (contact and (' · '..contact.name..(from~=item:getPath() and '（继承账号）' or '')) or ' · 未绑定账号')
        for _,child in ipairs(item:getChildren()) do visit(child,depth+1) end
    end
    visit(folder,0)
    LrDialogs.message('各拍摄文件夹返图状态',table.concat(lines,'\n')..'\n\n显示各文件夹最近任务，不代表新增照片也已发送。已提交不代表对方接收完成。')
end
