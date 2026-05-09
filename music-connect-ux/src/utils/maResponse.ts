export const unwrapResult = (payload: any) => {
  if (!payload || typeof payload !== 'object') return payload;
  if ('result' in payload) return payload.result;
  return payload;
};

export const pickArray = (value: any): any[] => {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== 'object') return [];
  const candidates = ['items', 'players', 'data', 'list'];
  for (const key of candidates) {
    if (Array.isArray((value as any)[key])) return (value as any)[key];
  }
  return [];
};
