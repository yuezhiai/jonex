import React from 'react';
import { useTranslation } from 'react-i18next';
import { Result, Button } from 'antd';
import { useNavigate } from 'react-router-dom';
import { getShellContext } from '@jonex/shell-sdk';

export default function NotFound() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  /** 返回首页：hosted 交给 shell 全局路由跳出子应用；standalone 用本应用路由兜底 */
  const handleBackHome = () => {
    const ctx = getShellContext();
    if (ctx?.navigate) ctx.navigate('/');
    else navigate('/');
  };

  return (
    <Result
      status="404"
      title="404"
      subTitle={t('common.pageNotFound')}
      extra={
        <Button type="primary" onClick={handleBackHome}>
          {t('common.backHome')}
        </Button>
      }
    />
  );
}
