/**
 * Mock 支付仅用于 dev/staging。生产环境（NODE_ENV=production）一律关闭。
 * 非生产须显式 PLATFORM_ALLOW_MOCK_PAY=1。
 */
export function isMockPayAllowed(): boolean {
  if (process.env['NODE_ENV'] === 'production') return false;
  return process.env['PLATFORM_ALLOW_MOCK_PAY'] === '1';
}
