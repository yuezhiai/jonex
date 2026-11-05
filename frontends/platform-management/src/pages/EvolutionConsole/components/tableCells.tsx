import React from 'react';

/** 演化台轻量表格单元格（smoke / candidate 卡共用）。 */
export const TH = ({ children }: { children: React.ReactNode }) => (
  <th style={{ textAlign: 'left', padding: '2px 8px', color: '#8c8c8c', fontWeight: 400 }}>{children}</th>
);

export const TD = ({ children }: { children: React.ReactNode }) => (
  <td style={{ padding: '2px 8px', verticalAlign: 'top' }}>{children}</td>
);
