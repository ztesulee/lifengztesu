local C=require 'Common'
local LrExportSession=import 'LrExportSession'
local LrDialogs=import 'LrDialogs'
local LrPathUtils=import 'LrPathUtils'
local LrFileUtils=import 'LrFileUtils'
local Settings=require 'FolderExportSettings'
return function()
    C.request('/health')
    if not C.catalog:getTargetPhoto() then error('请先选中要发送的照片或视频。') end
    local selected=C.catalog:getTargetPhotos()
    if #selected==0 then error('请先选中要发送的照片或视频。') end
    local options=Settings.show('账号来源：每张选中素材的元数据；可跨文件夹、跨人物。','发送已选中素材',#selected,true)
    if not options then return end
    local photos,rendered,contacts,groups={},{},{},{}
    for _,photo in ipairs(selected) do
        local video=photo:getRawMetadata('isVideo')
        if not video or options.includeVideo then
            local contact=C.photoContact(photo,true)
            contacts[photo.localIdentifier]=contact
            local qq=contact.qq~='';local wx=contact.wechat~=''
            local useQQ=qq and (not wx or contact.channel~='wechat')
            local label=contact.name..' · '..(useQQ and 'QQ：'..contact.qq or '微信：'..contact.wechat)
            groups[label]=(groups[label] or 0)+1
            if video then rendered[#rendered+1]={photo=photo,path=photo:getRawMetadata('path'),contact=contact}
            else photos[#photos+1]=photo end
        end
    end
    if #photos+#rendered==0 then error('没有需要发送的素材，请检查是否排除了全部视频。') end
    local lines={}
    for label,count in pairs(groups) do lines[#lines+1]=label..' · '..count..' 项' end
    table.sort(lines)
    if LrDialogs.confirm('导出并自动发送已选中素材',table.concat(lines,'\n')..'\n\n导出成功后将自动发送，请保持 QQ / 微信登录，勿操作鼠标键盘。','开始导出并发送','取消')~='ok' then return end
    local destination=LrPathUtils.child(options.destination,tostring(os.time())..'_'..tostring(math.random(100000,999999)))
    LrFileUtils.createAllDirectories(destination)
    C.exportProgress(0,#photos,'running')
    local exported=0
    if #photos>0 then
        local session=LrExportSession {photosToExport=photos,exportSettings=Settings.exportSettings(options,destination)}
        for _,rendition in session:renditions {stopIfCanceled=true} do
            local success,path=rendition:waitForRender()
            if not success then error('导出失败，整批未发送：'..tostring(path)) end
            rendered[#rendered+1]={photo=rendition.photo,path=path,contact=contacts[rendition.photo.localIdentifier]}
            exported=exported+1;C.exportProgress(exported,#photos,'running')
        end
    end
    if #rendered~=#photos+(options.includeVideo and (#selected-#photos) or 0) then error('导出未完成或被取消，整批未发送。') end
    C.exportProgress(exported,#photos,'done','图片导出完成：'..exported..' / '..#photos..'，准备创建发送任务')
    C.send(rendered,true,options.archiveDestination~='' and options.archiveDestination or options.destination,options.destination)
end
