return {
    schemaVersion = 5,
    metadataFieldsForPhotos = {
        { id='returned', title='是否返图', dataType='enum', version=1, searchable=true, browsable=true, values={
            { title='未返图' },
            { value='已返图', title='已返图' },
        } },
        { id='person', title='返图人物名称', dataType='string', searchable=true, browsable=true },
        { id='shootLocation', title='拍摄地址', dataType='string', version=1, searchable=true, browsable=true },
        { id='shootTime', title='拍摄时间', dataType='string', version=1, searchable=true, browsable=true },
        { id='qq', title='QQ 号码', dataType='string', searchable=true, browsable=true, version=2 },
        { id='wechat', title='微信号 / 搜索名', dataType='string', searchable=true, browsable=true, version=2 },
        { id='channel', title='发送渠道', dataType='enum', version=2, values={
            { value='自动', title='自动' },
            { value='QQ', title='QQ' },
            { value='微信', title='微信' },
        } },
    },
}
