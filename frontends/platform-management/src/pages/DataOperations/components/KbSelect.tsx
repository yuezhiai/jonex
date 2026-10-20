import { Select } from 'antd';
import { useTranslation } from 'react-i18next';
import { DO_KB_ALL } from '../../../constants/dataOperations';
import type { DoKbOption } from '../types';

interface Props {
  value: string;
  options: DoKbOption[];
  onChange: (value: string) => void;
  loading?: boolean;
}

/** 知识库筛选下拉，首项固定是「全部知识库」（值为 DO_KB_ALL） */
export default function KbSelect({ value, options, onChange, loading }: Props) {
  const { t } = useTranslation();

  return (
    <Select
      className="yx-do-kb-select"
      size="small"
      value={value}
      loading={loading}
      onChange={onChange}
      options={[
        { value: DO_KB_ALL, label: t('dataOperations.filter.allKb') },
        ...options.map((kb) => ({ value: kb.id, label: kb.name })),
      ]}
    />
  );
}
