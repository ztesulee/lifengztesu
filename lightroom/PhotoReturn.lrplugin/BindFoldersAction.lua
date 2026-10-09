local C=require 'Common'
return function()
    local photos=C.catalog:getTargetPhotos();local seen={}
    if #photos==0 then error('请先选中人物文件夹中的照片。') end
    for _,photo in ipairs(photos) do
        local folder=C.folder(photo)
        if not seen[folder] then if not C.bind(folder) then return end;seen[folder]=true end
    end
end
