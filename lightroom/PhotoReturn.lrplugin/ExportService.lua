local LrView=import 'LrView'
local C=require 'Common'
return {
    allowFileFormats={'JPEG'},
    allowColorSpaces={'sRGB','AdobeRGB','ProPhotoRGB'},
    exportPresetFields={{key='LR_format',default='JPEG'}},
    sectionsForTopOfDialog=function(f,properties)
        return {{title='按人物文件夹返图',
            f:static_text {title='照片按原文件夹绑定人物账号；导出 JPEG 后确认发送。支持多个文件夹混合 QQ / 微信。',wrap=true,width_in_chars=65},
            f:static_text {title='首次使用请运行返图后台，并在“文件 → 增效工具额外信息”中设置连接口令。',wrap=true,width_in_chars=65},
            f:static_text {title='请导出到独立目录，保留导出文件直到发送完成。视频使用 Lightroom 视频导出设置。',wrap=true,width_in_chars=65},
        }}
    end,
    processRenderedPhotos=function(context,exportContext)
        local ok,err=(import 'LrTasks').pcall(function()
            C.request('/health')
            local rendered={}
            for _,rendition in exportContext:renditions {stopIfCanceled=true} do
                local success,path=rendition:waitForRender()
                if not success then error('导出失败，整批未发送：'..tostring(path)) end
                rendered[#rendered+1]={photo=rendition.photo,path=path}
            end
            C.send(rendered,false,nil,rendered[1] and (import 'LrPathUtils').parent(rendered[1].path))
        end)
        if not ok then (import 'LrDialogs').message('返图失败',tostring(err),'critical') end
    end,
}
