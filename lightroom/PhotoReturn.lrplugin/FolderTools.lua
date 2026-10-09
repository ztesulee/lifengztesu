local C=require 'Common'
local LrPathUtils=import 'LrPathUtils'
local LrFunctionContext=import 'LrFunctionContext'
local LrBinding=import 'LrBinding'
local LrView=import 'LrView'
local LrDialogs=import 'LrDialogs'
local F={}
function F.choose(title)
    local photos=C.catalog:getTargetPhotos()
    if #photos==0 then error('请先选中该人物任意一张照片。') end
    local current=C.folder(photos[1]);local choices={}
    while current and current~='' do
        choices[#choices+1]={title=current,value=current}
        local parent=LrPathUtils.parent(current)
        if parent==current then break end
        current=parent
    end
    return LrFunctionContext.callWithContext('choose person folder',function(context)
        local p=LrBinding.makePropertyTable(context);p.folder=choices[math.min(2,#choices)].value
        local f=LrView.osFactory()
        if LrDialogs.presentModalDialog {title=title,actionVerb='选择此文件夹',contents=f:column {
            bind_to_object=p,spacing=8,
            f:static_text {title='请选择人物层级，例如“小渔”；其下可有多个拍摄日期文件夹。'},
            f:popup_menu {value=LrView.bind('folder'),items=choices,width_in_chars=70},
        }}=='ok' then return p.folder end
    end)
end
return F
