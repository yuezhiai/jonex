import React from 'react';
import { useTranslation } from 'react-i18next';
import { Result, Button } from 'antd';
import { useSearchParams } from 'react-router-dom';
import { getShellContext } from '@jonex/shell-sdk';

/** 错误页：读取 ?page=403 / ?page=404 区分展示；403 由权限守卫 redirect 而来 */
export default function ErrorPage() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const page = searchParams.get('page');
  const is403 = page === '403';

  const handleBackHome = () => {
    const ctx = getShellContext();
    if (ctx?.navigate) {
      // hosted：交给 shell 全局路由；standalone：整页跳回应用根
      ctx.navigate('/');
    } else {
      window.location.href = '/';
    }
  };

  return (
    <Result
      status={is403 ? '403' : '404'}
      title={is403 ? '403' : '404'}
      subTitle={is403 ? t('error.403') : t('common.pageNotFound')}
      extra={
        <Button type="primary" onClick={handleBackHome}>
          {t('common.backHome')}
        </Button>
      }
    />
  );
}
