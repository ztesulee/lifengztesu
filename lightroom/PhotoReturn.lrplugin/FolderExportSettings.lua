local C=require 'Common'
local LrFunctionContext=import 'LrFunctionContext'
local LrBinding=import 'LrBinding'
local LrView=import 'LrView'
local LrDialogs=import 'LrDialogs'
local LrPathUtils=import 'LrPathUtils'
local S={}

function S.exportSettings(options,destination)
    local quality=tonumber(options.quality)
    local edge=tonumber(options.longEdge)
    local resolution=tonumber(options.resolution)
    local limit=tonumber(options.fileSize or 5)
    local unit=options.fileSizeUnit or 'MB'
    if options.limitSize and (not limit or limit<=0 or (unit~='MB' and unit~='KB')) then error('文件大小上限必须为正数，单位选择 MB 或 KB。') end
    local limitKB=(limit or 5)*(unit=='MB' and 1024 or 1)
    if options.limitSize and (limitKB<1 or limitKB>2147483647) then error('文件大小上限超出范围，最小为 1 KB。') end
    if not quality or quality<1 or quality>100 then error('JPEG 品质必须在 1–100 之间。') end
    if options.resize and (not edge or edge<1 or edge>65000 or edge~=math.floor(edge)) then error('长边必须为 1–65000 的整数像素。') end
    if not resolution or resolution<1 or resolution>10000 then error('分辨率必须在 1–10000 之间。') end
    if options.colorSpace~='sRGB' and options.colorSpace~='AdobeRGB' and options.colorSpace~='ProPhotoRGB' then error('色彩空间不正确。') end
    return {
        LR_format='JPEG',LR_jpeg_quality=quality/100,LR_export_colorSpace=options.colorSpace,
        LR_jpeg_useLimitSize=options.limitSize or false,LR_jpeg_limitSize=math.floor(limitKB),
        LR_size_doConstrain=options.resize,LR_size_resizeType='longEdge',
        LR_size_maxHeight=edge or 2048,LR_size_maxWidth=edge or 2048,LR_size_units='pixels',
        LR_size_doNotEnlarge=options.noEnlarge,LR_size_resolution=resolution,LR_size_resolutionUnits='inch',
        LR_export_destinationType='specificFolder',LR_export_destinationPathPrefix=destination,
        LR_export_useSubfolder=false,LR_collisionHandling='rename',LR_reimportExportedPhoto=false,
        LR_outputSharpeningOn=false,
    }
end

function S.show(folder,person,count,selectedOnly)
    return LrFunctionContext.callWithContext('folder export settings',function(context)
        local p=LrBinding.makePropertyTable(context)
        local saved=C.prefs.folderExport or {}
        p.quality=saved.quality or 90;p.colorSpace=saved.colorSpace or 'sRGB'
        p.limitSize=saved.limitSize or false;p.fileSize=saved.fileSize or 5;p.fileSizeUnit=saved.fileSizeUnit or 'MB'
        p.qualityEnabled=not p.limitSize
        p:addObserver('limitSize',function()p.qualityEnabled=not p.limitSize end)
        p.resize=saved.resize or false;p.longEdge=saved.longEdge or 2048
        p.noEnlarge=saved.noEnlarge~=false;p.resolution=saved.resolution or 300
        p.includeVideo=saved.includeVideo~=false
        p.destination=saved.destination or LrPathUtils.child(LrPathUtils.getStandardFilePath('documents'),'Lightroom返图导出')
        p.archiveDestination=saved.archiveDestination or ''
        local factory=LrView.osFactory()
        local textControls={static_text=true,edit_field=true,push_button=true,checkbox=true,popup_menu=true}
        local f=setmetatable({},{__index=function(_,kind)
            return function(_,properties)
                if textControls[kind] then properties.font={name='Microsoft YaHei UI',size=13} end
                return factory[kind](factory,properties)
            end
        end})
        local bind=LrView.bind
        local contents=f:column {bind_to_object=p,spacing=8,
            f:static_text {title=person..' · '..count..(selectedOnly and ' 项已选中素材' or ' 项素材（当前文件夹，不含子文件夹）')},
            f:static_text {title=folder,width_in_chars=70,wrap=true},
            f:group_box {title='导出位置',fill_horizontal=1,
                f:row {
                    f:edit_field {value=bind('destination'),width_in_chars=52},
                    f:push_button {title='浏览…',action=function()
                        local paths=LrDialogs.runOpenPanel {title='选择 JPEG 导出位置',canChooseFiles=false,canChooseDirectories=true,allowsMultipleSelection=false}
                        if paths and paths[1] then p.destination=paths[1] end
                    end},
                },
                f:static_text {title='素材按“人物名称_微信或QQ号码”保存到子文件夹；同名且微信号/QQ号码相同的子文件夹会被清空覆盖，仅保留本次素材。',wrap=true,width_in_chars=70},
            },
            f:group_box {title='文件设置',fill_horizontal=1,
                f:static_text {title='压缩包保存路径（留空使用导出位置；同名且微信号/QQ号码相同的子文件夹会被清空覆盖）',wrap=true,width_in_chars=70},
                f:row {
                    f:edit_field {value=bind('archiveDestination'),width_in_chars=52},
                    f:push_button {title='浏览…',action=function()
                        local paths=LrDialogs.runOpenPanel {title='选择压缩包保存位置',canChooseFiles=false,canChooseDirectories=true,allowsMultipleSelection=false}
                        if paths and paths[1] then p.archiveDestination=paths[1] end
                    end},
                },
                f:static_text {title='图像格式：JPEG'},
                f:row {f:static_text {title='品质',width_in_chars=12},
                    f:slider {value=bind('quality'),enabled=bind('qualityEnabled'),min=1,max=100,integral=true,width=240},
                    f:edit_field {value=bind('quality'),enabled=bind('qualityEnabled'),width_in_chars=6}},
                f:row {
                    f:checkbox {title='文件大小限制为（每张）',value=bind('limitSize')},
                    f:edit_field {value=bind('fileSize'),enabled=bind('limitSize'),width_in_chars=8},
                    f:popup_menu {value=bind('fileSizeUnit'),enabled=bind('limitSize'),items={{title='MB',value='MB'},{title='KB',value='KB'}}},
                },
                f:static_text {title='由 Lightroom 自动调整 JPEG 品质以满足上限，实际文件可能更小；上限太小可能导出失败。'},
                f:row {f:static_text {title='色彩空间',width_in_chars=12},
                    f:popup_menu {value=bind('colorSpace'),items={{title='sRGB（推荐）',value='sRGB'},{title='Adobe RGB',value='AdobeRGB'},{title='ProPhoto RGB',value='ProPhotoRGB'}}}},
            },
            f:group_box {title='图像大小',fill_horizontal=1,
                f:checkbox {title='调整长边尺寸（未勾选时保持原尺寸）',value=bind('resize')},
                f:row {f:static_text {title='长边',width_in_chars=12},
                    f:edit_field {value=bind('longEdge'),enabled=bind('resize'),width_in_chars=8},f:static_text {title='像素'}},
                f:checkbox {title='不放大原图',value=bind('noEnlarge'),enabled=bind('resize')},
                f:row {f:static_text {title='分辨率',width_in_chars=12},
                    f:edit_field {value=bind('resolution'),width_in_chars=8},f:static_text {title='像素 / 英寸（不会单独改变像素尺寸）'}},
            },
            f:group_box {title='视频',fill_horizontal=1,
                f:checkbox {title=selectedOnly and '包含已选中的视频' or '包含该文件夹的视频',value=bind('includeVideo')},
                f:static_text {title='此入口发送原视频，不转换格式或压缩视频。'},
            },
            f:static_text {title=selectedOnly and '导出前确认元数据账号，导出成功后自动加入发送队列。' or '导出完成后会再次确认人物、接收账号和数量，再加入 QQ / 微信队列。'},
        }
        while true do
            if LrDialogs.presentModalDialog {title='人物文件夹 · 导出与返图设置',contents=f:scrolled_view {width=840,height=650,contents},actionVerb='开始导出'}~='ok' then return nil end
            local options={quality=p.quality,colorSpace=p.colorSpace,resize=p.resize,longEdge=p.longEdge,
                noEnlarge=p.noEnlarge,resolution=p.resolution,includeVideo=p.includeVideo,destination=p.destination,
                limitSize=p.limitSize,fileSize=p.fileSize,fileSizeUnit=p.fileSizeUnit,
                archiveDestination=(p.archiveDestination or ''):match('^%s*(.-)%s*$')}
            local ok,err=pcall(function()
                if not p.destination or p.destination:match('^%s*$') then error('请选择导出位置。') end
                S.exportSettings(options,p.destination)
            end)
            if ok then C.prefs.folderExport=options;return options end
            LrDialogs.message('导出设置不正确',tostring(err),'critical')
        end
    end)
end
return S
