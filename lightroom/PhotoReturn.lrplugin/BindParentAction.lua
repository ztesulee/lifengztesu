local C=require 'Common'
return function()
    local folder=(require 'FolderTools').choose('绑定人物父文件夹')
    if folder then C.bind(folder) end
end
