import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { isImagePath, resolveFeishuFileType, checkFileForDelivery } from '../src/delivery.js';

// --- Delivery helper tests (no pool needed) ---

describe('delivery helpers', () => {
  const testDir = join(tmpdir(), 'relay-test-tool-files');

  beforeEach(() => {
    mkdirSync(testDir, { recursive: true });
  });

  afterEach(() => {
    rmSync(testDir, { recursive: true, force: true });
  });

  it('isImagePath detects common image extensions', () => {
    expect(isImagePath('/tmp/photo.jpg')).toBe(true);
    expect(isImagePath('/tmp/photo.png')).toBe(true);
    expect(isImagePath('/tmp/photo.gif')).toBe(true);
    expect(isImagePath('/tmp/photo.webp')).toBe(true);
    expect(isImagePath('/tmp/photo.JPEG')).toBe(true);
    expect(isImagePath('/tmp/doc.pdf')).toBe(false);
    expect(isImagePath('/tmp/data.csv')).toBe(false);
    expect(isImagePath('/tmp/script.sh')).toBe(false);
  });

  it('resolveFeishuFileType maps extensions correctly', () => {
    expect(resolveFeishuFileType('/tmp/doc.pdf')).toBe('pdf');
    expect(resolveFeishuFileType('/tmp/sheet.xls')).toBe('xls');
    expect(resolveFeishuFileType('/tmp/sheet.xlsx')).toBe('xls');
    expect(resolveFeishuFileType('/tmp/pres.pptx')).toBe('ppt');
    expect(resolveFeishuFileType('/tmp/audio.opus')).toBe('opus');
    // mp4 → 'stream' to avoid Feishu API 230055 (im.video.create unavailable)
    expect(resolveFeishuFileType('/tmp/video.mp4')).toBe('stream');
    expect(resolveFeishuFileType('/tmp/clip.mov')).toBe('stream');
    expect(resolveFeishuFileType('/tmp/data.csv')).toBe('stream');
    expect(resolveFeishuFileType('/tmp/report.md')).toBe('stream');
  });

  it('checkFileForDelivery accepts valid files', () => {
    const filePath = join(testDir, 'test.txt');
    writeFileSync(filePath, 'hello world');
    const result = checkFileForDelivery(filePath);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.size).toBe(11);
    }
  });

  it('checkFileForDelivery rejects missing files', () => {
    const result = checkFileForDelivery('/nonexistent/file.txt');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toContain('not found');
    }
  });

  it('checkFileForDelivery rejects empty files', () => {
    const filePath = join(testDir, 'empty.txt');
    writeFileSync(filePath, '');
    const result = checkFileForDelivery(filePath);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toContain('Empty');
    }
  });

  it('checkFileForDelivery accepts small image files', () => {
    const filePath = join(testDir, 'small.png');
    writeFileSync(filePath, 'fake png data');
    const result = checkFileForDelivery(filePath);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.size).toBeGreaterThan(0);
    }
  });
});
