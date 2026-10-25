let fallbackSequence = 0;

export function newId(): string {
  const cryptoApi = globalThis.crypto;

  if (typeof cryptoApi?.randomUUID === 'function') {
    try {
      return cryptoApi.randomUUID();
    } catch {
      // Continue with a compatible UUID v4 implementation.
    }
  }

  const bytes = new Uint8Array(16);
  let hasSecureRandom = false;

  if (typeof cryptoApi?.getRandomValues === 'function') {
    try {
      cryptoApi.getRandomValues(bytes);
      hasSecureRandom = true;
    } catch {
      // Fall back to non-cryptographic entropy for local object identifiers.
    }
  }

  if (!hasSecureRandom) fillFallbackBytes(bytes);

  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;

  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0'));
  return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10).join('')}`;
}

function fillFallbackBytes(bytes: Uint8Array): void {
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Math.floor(Math.random() * 256);
  }

  let uniqueness = BigInt(Date.now()) ^ (BigInt(++fallbackSequence) << 48n);
  for (let index = 0; index < 8; index += 1) {
    bytes[index] = bytes[index]! ^ Number(uniqueness & 0xffn);
    uniqueness >>= 8n;
  }
}
