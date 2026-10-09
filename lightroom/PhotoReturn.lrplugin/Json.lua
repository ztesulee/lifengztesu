local J = {}
local function quote(s)
    return '"' .. s:gsub('[%z\1-\31\\"]', function(c)
        local escape = { ['"']='\\"', ['\\']='\\\\', ['\n']='\\n', ['\r']='\\r', ['\t']='\\t' }
        return escape[c] or string.format('\\u%04x', c:byte())
    end) .. '"'
end
function J.encode(v)
    local t = type(v)
    if t == 'string' then return quote(v) end
    if t == 'boolean' or t == 'number' then return tostring(v) end
    if t == 'nil' then return 'null' end
    assert(t == 'table', 'unsupported JSON type')
    local out = {}
    if #v > 0 then
        for _, value in ipairs(v) do out[#out+1] = J.encode(value) end
        return '[' .. table.concat(out, ',') .. ']'
    end
    for key, value in pairs(v) do out[#out+1] = quote(key) .. ':' .. J.encode(value) end
    return '{' .. table.concat(out, ',') .. '}'
end
local function utf8(code)
    if code < 128 then return string.char(code) end
    if code < 2048 then return string.char(192+math.floor(code/64),128+code%64) end
    return string.char(224+math.floor(code/4096),128+math.floor(code/64)%64,128+code%64)
end
function J.decode(text)
    local pos = 1
    local function skip() local _, last = text:find('^%s*', pos); pos = (last or pos-1)+1 end
    local parse
    local function str()
        pos=pos+1; local out={}
        while pos <= #text do
            local c=text:sub(pos,pos);pos=pos+1
            if c=='"' then return table.concat(out) end
            if c=='\\' then
                c=text:sub(pos,pos);pos=pos+1
                local escapes={['"']='"',['\\']='\\',['/']='/',b='\b',f='\f',n='\n',r='\r',t='\t'}
                if c=='u' then local n=assert(tonumber(text:sub(pos,pos+3),16));pos=pos+4;out[#out+1]=utf8(n)
                else out[#out+1]=assert(escapes[c],'invalid escape') end
            else out[#out+1]=c end
        end
        error('unterminated string')
    end
    parse=function()
        skip();local c=text:sub(pos,pos)
        if c=='"' then return str() end
        if c=='{' or c=='[' then
            local object=c=='{';local close=object and '}' or ']';pos=pos+1;skip();local out={}
            if text:sub(pos,pos)==close then pos=pos+1;return out end
            while true do
                local key
                if object then skip();assert(text:sub(pos,pos)=='"');key=str();skip();assert(text:sub(pos,pos)==':');pos=pos+1 end
                local value=parse();if object then out[key]=value else out[#out+1]=value end
                skip();c=text:sub(pos,pos);pos=pos+1
                if c==close then return out end
                assert(c==',','invalid separator')
            end
        end
        for literal,value in pairs({['true']=true,['false']=false}) do
            if text:sub(pos,pos+#literal-1)==literal then pos=pos+#literal;return value end
        end
        if text:sub(pos,pos+3)=='null' then pos=pos+4;return nil end
        local number=text:match('^-?%d+%.?%d*[eE]?[+-]?%d*',pos)
        assert(number and #number>0,'invalid JSON');pos=pos+#number;return assert(tonumber(number))
    end
    local result=parse();skip();assert(pos>#text,'trailing JSON');return result
end
return J
