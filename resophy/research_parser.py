"""Bounded MinerU archive conversion; no archive paths are extracted to disk."""
import hashlib
import io
from pathlib import PurePosixPath
import re
import zipfile
from urllib.parse import urljoin, urlparse

from resophy.research_store import ResearchError


def normalized_png(content):
    from PIL import Image
    with Image.open(io.BytesIO(content)) as image:
        if image.format not in {'PNG', 'JPEG'} or max(image.size) > 4096:
            raise ValueError()
        output = io.BytesIO()
        image.convert('RGB').save(output, format='PNG')
    return output.getvalue()


def trusted_asset_url(url):
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in {None, 443}
            or parsed.hostname not in {'cdn-mineru.openxlab.org.cn', 'oss-mineru.openxlab.org.cn',
                                       'mineru.oss-cn-shanghai.aliyuncs.com'}):
        raise ResearchError('parser_asset_url', '解析产物地址不在允许的 MinerU HTTPS 资源域名中。')
    return url


def read_agent_markdown(text, markdown_url, download):
    trusted_asset_url(markdown_url)
    images, replacements = [], {}
    for reference in re.findall(r'!\[[^\]]*\]\(([^\s)]+)\)', text):
        if reference in replacements:
            continue
        if len(images) >= 64:
            raise ResearchError('parser_output', '解析图片数量超过限制。')
        url = trusted_asset_url(urljoin(markdown_url, reference))
        try:
            png = normalized_png(download(url, maximum=4 * 1024 * 1024))
        except (ValueError, OSError):
            raise ResearchError('parser_output', '解析图片格式或尺寸无效。') from None
        placeholder = 'research-figure:' + hashlib.sha256(png).hexdigest()
        replacements[reference] = placeholder
        images.append((placeholder, png))
        if sum(len(image) for _, image in images) > 20 * 1024 * 1024:
            raise ResearchError('parser_output', '解析图片总量超过限制。')
    for reference, placeholder in replacements.items():
        text = text.replace('](' + reference + ')', '](' + placeholder + ')')
    return text, images


def read_parser_archive(content):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zipped:
            entries = zipped.infolist()
            names = [entry.filename for entry in entries]
            if (len(entries) > 1000 or len(names) != len(set(names))
                    or sum(entry.file_size for entry in entries) > 80 * 1024 * 1024):
                raise ValueError()
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                        or ':' in entry.filename or entry.flag_bits & 1
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError()
            matches = [entry for entry in entries if PurePosixPath(entry.filename).name == 'full.md']
            if len(matches) != 1 or matches[0].file_size > 1024 * 1024:
                raise ValueError()
            text = zipped.read(matches[0]).decode('utf-8')
            prefix = PurePosixPath(matches[0].filename).parent
            images, replacements = [], {}
            for reference in re.findall(r'!\[[^\]]*\]\(([^\s)]+)\)', text):
                name = str(prefix / reference)
                if reference in replacements:
                    continue
                if name not in names or len(images) >= 64:
                    raise ValueError()
                entry = zipped.getinfo(name)
                if entry.file_size > 4 * 1024 * 1024 or PurePosixPath(name).suffix.lower() not in {'.png', '.jpg', '.jpeg'}:
                    raise ValueError()
                png = normalized_png(zipped.read(entry))
                placeholder = 'research-figure:' + hashlib.sha256(png).hexdigest()
                replacements[reference] = placeholder
                images.append((placeholder, png))
            for reference, placeholder in replacements.items():
                text = text.replace('](' + reference + ')', '](' + placeholder + ')')
            if sum(len(image) for _, image in images) > 20 * 1024 * 1024:
                raise ValueError()
            return text, images
    except (ValueError, KeyError, zipfile.BadZipFile, UnicodeDecodeError, OSError):
        raise ResearchError('parser_output', '解析包的格式、路径、图片或大小无效；未发布结果。') from None
