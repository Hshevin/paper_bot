import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from io import BytesIO
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from paperbot.models import Paper
from paperbot.reading import (
    CHUNK_CHARS, MAX_TEXT_CHARS, ReadingError, chunk_text, extract_text, read_paper,
)


NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
PAPER = Paper("2610.00001", 2, "Test paper", "Abstract", (), ("cs.AI",), NOW, NOW)


class TextTests(unittest.TestCase):
    def test_chunks_preserve_all_characters_at_boundaries(self):
        text = "第一段\n" + "abcdefghijk\n" * 3000 + "\n最后一段"
        chunks = chunk_text(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(0 < len(chunk) <= CHUNK_CHARS for chunk in chunks))

    def test_text_limit_checked_before_partition(self):
        with self.assertRaises(ReadingError):
            chunk_text("a" * (MAX_TEXT_CHARS + 1))

    def test_pdf_without_text_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.write(path)
            with self.assertRaisesRegex(ReadingError, "文字"):
                extract_text(path)

    def test_pdf_page_and_text_limits(self):
        for pages, message in [([Mock()] * 301, "300"),
                               ([Mock(extract_text=lambda: "x" * (MAX_TEXT_CHARS + 1))], "240000")]:
            with self.subTest(message=message), patch("paperbot.reading.PdfReader") as reader:
                reader.return_value.is_encrypted = False
                reader.return_value.pages = pages
                with self.assertRaisesRegex(ReadingError, message):
                    extract_text(Path("fake.pdf"))


class Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class Response:
    status = 200
    content_length = None

    def __init__(self, chunks):
        self.content = Content(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class ReadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.session = Mock()
        self.session.get.return_value = Response([b"%PDF-1.4\n", b"stub"])
        self.generate = AsyncMock(return_value="以原文为依据的中文报告")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_short_paper_downloads_fixed_url_then_uses_cache(self):
        old_paper = replace(PAPER, id="hep-th/9901001")
        with patch("paperbot.reading.extract_text", return_value="[第 1 页]\n有效正文和证据。"):
            report = await read_paper(old_paper, self.directory, self.session, self.generate)
            await read_paper(old_paper, self.directory, self.session, self.generate)
        self.assertIn("中文报告", report)
        self.session.get.assert_called_once()
        self.assertEqual(self.session.get.call_args.args[0], "https://arxiv.org/pdf/hep-th/9901001v2")
        self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(len(list(self.directory.rglob("*.pdf"))), 1)
        self.assertEqual(self.generate.await_count, 2)

    async def test_no_model_call_if_text_extraction_fails(self):
        with patch("paperbot.reading.extract_text", side_effect=ReadingError("无法提取文字")):
            with self.assertRaises(ReadingError):
                await read_paper(PAPER, self.directory, self.session, self.generate)
        self.generate.assert_not_awaited()

    async def test_long_paper_uses_every_chunk_then_synthesizes(self):
        text = "x" * MAX_TEXT_CHARS
        self.generate.side_effect = [f"片段 {i} 的原文证据" for i in range(14)] + ["完整综合报告"]
        with patch("paperbot.reading.extract_text", return_value=text):
            report = await read_paper(PAPER, self.directory, self.session, self.generate)
        self.assertEqual(report, "完整综合报告")
        calls = self.generate.await_args_list
        self.assertEqual(len(calls), 15)
        for index, chunk in enumerate(chunk_text(text)):
            self.assertIn(chunk, calls[index].args[1])
        for index in range(14):
            self.assertIn(f"片段 {index} 的原文证据", calls[-1].args[1])

    async def test_oversize_notes_abort_before_synthesis(self):
        self.generate.return_value = "x" * 5001
        with patch("paperbot.reading.extract_text", return_value="x" * (CHUNK_CHARS + 1)):
            with self.assertRaisesRegex(ReadingError, "5000"):
                await read_paper(PAPER, self.directory, self.session, self.generate)
        self.assertEqual(self.generate.await_count, 1)

    async def test_stream_limit_removes_partial_cache(self):
        self.session.get.return_value = Response([b"%PDF-", b"x" * 20])
        with patch("paperbot.reading.MAX_PDF_BYTES", 10):
            with self.assertRaises(ReadingError):
                await read_paper(PAPER, self.directory, self.session, self.generate)
        self.assertEqual([p for p in self.directory.rglob("*") if p.is_file()], [])
        self.generate.assert_not_awaited()

    async def test_invalid_id_never_fetches(self):
        with self.assertRaises(ReadingError):
            await read_paper(replace(PAPER, id="../../secrets"), self.directory, self.session, self.generate)
        self.session.get.assert_not_called()

    async def test_real_pdf_text_reaches_model_with_page_reference(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 10 30 Td (Evidence from experiments.) Tj ET")
        page[NameObject("/Contents")] = content
        buffer = BytesIO()
        writer.write(buffer)
        self.session.get.return_value = Response([buffer.getvalue()])
        await read_paper(PAPER, self.directory, self.session, self.generate)
        self.assertIn("Evidence from experiments.", self.generate.await_args.args[1])
        self.assertIn("[PDF 第 1 页]", self.generate.await_args.args[1])

    async def test_non_pdf_response_is_never_cached_or_sent_to_model(self):
        self.session.get.return_value = Response([b"<html>maintenance</html>"])
        with self.assertRaises(ReadingError):
            await read_paper(PAPER, self.directory, self.session, self.generate)
        self.generate.assert_not_awaited()
        self.assertEqual([p for p in self.directory.rglob("*") if p.is_file()], [])

    async def test_cancelled_download_removes_temporary_file(self):
        async def interrupted(size):
            yield b"%PDF-"
            raise asyncio.CancelledError()
        self.session.get.return_value.content.iter_chunked = interrupted
        with self.assertRaises(asyncio.CancelledError):
            await read_paper(PAPER, self.directory, self.session, self.generate)
        self.assertEqual([p for p in self.directory.rglob("*") if p.is_file()], [])
        self.generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
