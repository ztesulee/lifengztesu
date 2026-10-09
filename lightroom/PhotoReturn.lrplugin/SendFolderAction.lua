local C=require 'Common'
local LrExportSession=import 'LrExportSession'
local LrDialogs=import 'LrDialogs'
local LrPathUtils=import 'LrPathUtils'
local LrFileUtils=import 'LrFileUtils'
local Settings=require 'FolderExportSettings'
return function()
    C.request('/health')
    local selected=C.catalog:getTargetPhotos()
    if #selected==0 then error('请先选中人物文件夹中的一张照片。') end
    local folderPath=C.folder(selected[1])
    for _,photo in ipairs(selected) do if C.folder(photo)~=folderPath then error('此菜单一次发送一个人物文件夹；多文件夹请使用导出窗口。') end end
    local folder=C.catalog:getFolderByPath(folderPath)
    if not folder then error('目录文件中未找到这个文件夹。') end
    local contact=C.photoContact(selected[1]) or C.bind(folderPath)
    if not contact then return end
    local folderPhotos=folder:getPhotos(false)
    local options=Settings.show(folderPath,contact.name,#folderPhotos)
    if not options then return end
    local destination=options.destination
    destination=LrPathUtils.child(destination,tostring(os.time())..'_'..tostring(math.random(100000,999999)))
    LrFileUtils.createAllDirectories(destination)
    local photos,rendered={},{}
    for _,photo in ipairs(folderPhotos) do
        if photo:getRawMetadata('isVideo') then
            if options.includeVideo then rendered[#rendered+1]={photo=photo,path=photo:getRawMetadata('path')} end
        else photos[#photos+1]=photo end
    end
    C.exportProgress(0,#photos,'running')
    local exported=0
    local expected=#photos+#rendered
    if #photos>0 then
        local session=LrExportSession {photosToExport=photos,exportSettings=Settings.exportSettings(options,destination)}
        for _,rendition in session:renditions {stopIfCanceled=true} do
            local success,path=rendition:waitForRender()
            if not success then error('导出失败，整批未发送：'..tostring(path)) end
            rendered[#rendered+1]={photo=rendition.photo,path=path}
            exported=exported+1;C.exportProgress(exported,#photos,'running')
        end
    end
    if #rendered~=expected then error('导出未完成或被取消，整批未发送。') end
    C.exportProgress(exported,#photos,'done','图片导出完成：'..exported..' / '..#photos..'，等待确认发送')
    C.send(rendered,false,options.archiveDestination~='' and options.archiveDestination or options.destination,options.destination)
end
