import { useEffect, useRef, useState } from 'react';
import {
  Button,
  Col,
  Collapse,
  Divider,
  Drawer,
  Flex,
  Form,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Typography,
  message,
  theme,
} from 'antd';
import { ApiOutlined, CopyOutlined, SaveOutlined, SettingOutlined } from '@ant-design/icons';
import copyToClipboard from 'copy-to-clipboard';
import { useTranslation } from 'react-i18next';
import SectionHeading from '../../components/SectionHeading';
import { fmtDateTime } from '../../utils/datetime';
import {
  LOGIN_FIELD_MAPPING,
  getLoginPolicyLabel,
  getSsoRealm,
  isSsoNotProvisioned,
  readProbeTarget,
  testConnection,
  updateIntegrationConfig,
  type LoginPolicy,
  type SsoConfigUpdate,
  type SsoRealmResponse,
  type SsoStatus,
} from '../../api/identityIntegration';

/**
 * 「单点登录配置」侧边抽屉（E2 保存 / E3 测试连接）。
 *
 * 布局与同步抽屉同构：基础配置（启用开关 + 首次登录账号策略）与可折叠的高级配置
 * （Realm 参数与字段映射 + 测试连接；其中只有「服务地址」可改，其余只读），
 * 底部「取消」+「保存配置」。
 *
 * 两处交互约束来自产品要求，一期后端不强制，由前端把住：
 * - 启用前必须先完成一次连接测试（`last_verified_at` 非空），否则企业用户会登录失败；
 * - 由启用改为停用需要二次确认。
 *
 * 「测试连接」只探**已保存**的配置（不带表单快照，见 onTestConnection），于是输入框里的
 * 表单值可能与它真正要探的地址分叉。这个分叉由四条提示共同讲清：
 * ① 按钮下方优先显示后端三级解析（白名单反查 → KEYCLOAK_SERVER_URL → issuer_url）后回传的
 *    **完整**探测地址（`effective_probe_target`；老后端不返回该字段时回落为「已保存的
 *    issuer_url + OIDC 发现端点」）；
 * ② 输入框被改成与已保存值不一致时（`issuerUrlDirty`），紧贴输入框显示「有未保存的更改」；
 * ③ 同一状态下点「测试连接」先弹确认，把「将探已保存的地址 X」再说一遍才放行 —— 专治
 *    「改完没保存就去测、报错只说 SSO 连接失败」这条已被实测坐实的误报路径（2026-09-28
 *    用户改回地址但未保存，连点 16 次稳定 5005，界面显示 A、系统探 B）；
 * ④ 失败时再把后端回报的实际出站地址（`errorDetails.target`）回显到报错里。
 * ①④ 取自同一次解析，逐字可比对。
 *
 * 「服务地址」另有一条**不能清空**的约束：`tenant_realm_map.issuer_url` 列 NOT NULL，后端
 * 也不接受空串（`validate_sso_url("")` → 1007）。用户把**已保存的非空地址**清空时，输入框下
 * 给红字并拦住保存（`issuerUrlCleared`）—— 从前是静默不生效（空串不放进请求体、界面照样弹
 * 「保存成功」），用户以为清掉了、实际没变。
 *
 * Client Secret 由部署侧注入（平台级单 client），前端只读展示说明、不提供输入。
 * Realm / Client ID / 回调地址同样只读：Realm 是后端反查租户的唯一键、Client ID 没有消费方、
 * 回调地址是部署侧全局配置，开放编辑都只会制造跨租户或静默失效的风险。
 */
const { Text } = Typography;
const { useToken } = theme;

interface SsoConfigFormValues {
  enabled: boolean;
  loginPolicy: LoginPolicy;
  /**
   * 服务地址（`issuer_url`）。值由 realmState 就绪时补写，未就绪时字段不渲染、值为 undefined。
   * 允许 undefined：提交时空值不放进请求体（= 保持原值，后端语义如此）。
   * **但不允许把已保存的非空地址清空** —— 那是一条不存在的操作，见组件 docstring。
   */
  issuerUrl?: string;
}

interface SsoConfigDrawerProps {
  open: boolean;
  /** E1 的 SSO 段，作为表单初值与验证状态来源 */
  status: SsoStatus | null;
  onClose: () => void;
  /** 保存 / 测试成功后触发，由页面重新拉取 E1 */
  onChanged: () => void;
}

/** Realm 只读展示的四态；用联合类型避免 loading / error 同时为真的非法组合 */
type RealmState =
  | { kind: 'loading' }
  | { kind: 'unconfigured' }
  | { kind: 'error' }
  | { kind: 'ready'; data: SsoRealmResponse };

/** 字段映射表的一行：企业侧字段 → Jonex字段，末列标注该映射的性质 */
interface MappingRow {
  key: string;
  /** Jonex侧的业务字段名（用户名 / 姓名 / 邮箱） */
  fieldName: string;
  /** OIDC 返回的 claim 名 */
  providerField: string;
  /** Jonex users 表的实际列名；`sub` 不落列，留空 */
  localField: string;
  rule: string;
}

export default function SsoConfigDrawer({ open, status, onClose, onChanged }: SsoConfigDrawerProps) {
  const { t } = useTranslation();
  const { token } = useToken();
  const [form] = Form.useForm<SsoConfigFormValues>();
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [realmState, setRealmState] = useState<RealmState>({ kind: 'loading' });

  // Realm 信息每次打开都重新拉取：除服务地址外前端改不了它，但运维侧可能改动（典型是 status 转停用）。
  // 不复用表单那套 inited ref —— 那个只在首次打开初始化，避免覆盖用户已填内容。
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setRealmState({ kind: 'loading' });
    getSsoRealm()
      .then((data) => {
        if (!cancelled) setRealmState({ kind: 'ready', data });
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        // 租户未开通 Realm 时后端返回 1002，属正常分支而非故障
        setRealmState({ kind: isSsoNotProvisioned(e) ? 'unconfigured' : 'error' });
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // 服务地址的初值是这个 realmState 给的，而它**异步**才返回，赶不上下面 inited 那一次表单初始化，
  // 只能在数据到位时补写一次。
  //
  // 拿什么当「已经写过」的判据，是本组件最容易写错的一处，取舍如下：
  // - 不能改成「每次 realmState 变化就写」：realmState 与 status 是两条独立的异步流，
  //   测试连接成功会触发页面 refreshAll（status 变化）；哪天 realmState 也被并进那条流，
  //   回写就会把用户刚改完、还没保存的地址冲掉 —— 正是 inited 注释里要避开的同一个坑。
  // - 也不能用「本次打开是否写过」的布尔开关：关闭抽屉时不清空 realmState，重新打开的那一帧
  //   会带着上一轮的 ready 数据（随后才被置回 loading），布尔开关会把它当作首写而灌入旧值，
  //   等新数据回来时又因「已写过」被跳过，输入框就永远停在旧地址上。
  // - 改用数据对象本身判重：陈旧数据与已应用的是同一个对象 → 跳过；新一次请求返回的是新对象
  //   → 补写。前提是打开期间不会重新拉取 realmState（当前只有 open 变化才拉）；将来若加了
  //   「刷新 Realm」这类入口，需要在这里再补一道「用户已改过就不回写」的判断。
  const appliedRealm = useRef<SsoRealmResponse | null>(null);
  useEffect(() => {
    if (realmState.kind !== 'ready') return;
    if (appliedRealm.current === realmState.data) return;
    appliedRealm.current = realmState.data;
    form.setFieldValue('issuerUrl', realmState.data.issuer_url ?? '');
  }, [realmState, form]);

  const dl = (key: string, options?: Record<string, unknown>) => t(`tenantManagement.ssoDrawer.${key}`, options);

  // 「测试连接」真正要探的地址 = 已保存的 issuer_url（它只探已存配置，不带表单快照）。
  // 与输入框里的表单值**不是一回事**：用户改完不保存时两者会分叉，所以要显式写给用户看。
  const savedIssuerUrl =
    realmState.kind === 'ready' ? (realmState.data.issuer_url ?? '').trim() : '';

  // 后端三级解析后回传的**真实**探测地址，优先于上面的 savedIssuerUrl：探测是容器内出站，
  // 而 issuer_url 是浏览器视角的地址，两者在新解析规则下会分叉。老后端不返回该字段时为
  // undefined，`?? ''` 后为空串 → 自动回落到 savedIssuerUrl 那条老路径。
  const effectiveProbeTarget =
    realmState.kind === 'ready' ? (realmState.data.effective_probe_target ?? '').trim() : '';

  // 输入框当前值。直读表单 store，不再另存一份 state —— 上面的 realm 补写走的是
  // setFieldValue（不触发 onValuesChange），另存一份就会出现「补写后 state 仍是旧值」的错位。
  // realm 未就绪、或补写尚未发生（effect 要等首次 render 之后才跑）时是 undefined：下面两个
  // 判据都因此不成立，不会在那一帧闪一下红字。
  const issuerUrlDraft = Form.useWatch('issuerUrl', form);

  // 「有未保存的更改」：输入框值与已保存值不一致。`issuerUrlDraft !== undefined` 这道闸必须有 ——
  // 补写（setFieldValue）发生在首次 render 之后，那一帧 draft 还是 undefined，不闸住就会被当成
  // 「值 = 空」，对着一个还没填过的框报「有未保存的更改」。
  const issuerUrlDirty =
    realmState.kind === 'ready' &&
    issuerUrlDraft !== undefined &&
    (issuerUrlDraft ?? '').trim() !== savedIssuerUrl;

  // 「不能清空」：已保存值非空、而输入框被清空。**从 issuerUrlDirty 派生**，白捡上面那道
  // undefined 闸，免得两条判据各写一遍、漏掉一处。只在「本来有值」时才拦 —— 已保存值为空时不拦，
  // 否则会连累「只改启用开关」的保存（空值不传这条语义存在的理由正是为了它）。
  const issuerUrlCleared =
    issuerUrlDirty && savedIssuerUrl !== '' && !(issuerUrlDraft ?? '').trim();

  // 仅在「打开」这一刻用最新的 E1 状态初始化表单。
  // 不能在 status 变化时重置：测试连接成功会触发页面 refreshAll，那会把用户刚填的内容清掉。
  const inited = useRef(false);
  useEffect(() => {
    if (open && !inited.current) {
      form.setFieldsValue({
        enabled: status?.enabled ?? false,
        loginPolicy: status?.login_policy ?? 'auto_create',
        // 服务地址的初值这一刻还拿不到（realmState 要等打开后才返回，由上面的 effect 补写），
        // 这里先清空。表单 store 不随抽屉关闭清空，而这个抽屉会被不同租户复用
        // （租户详情是同一路由、只换 params.id，组件不重挂），不清就会把上一个租户的地址
        // 带进下一个租户的保存请求里。
        issuerUrl: undefined,
      });
    }
    inited.current = open;
  }, [open, status, form]);

  const submit = async (values: SsoConfigFormValues) => {
    setSaving(true);
    try {
      const sso: SsoConfigUpdate = {
        enabled: values.enabled,
        login_policy: values.loginPolicy,
      };
      // 服务地址空值不放进请求体（后端语义：不传 = 保持原值；空串会被后端判 1007）。
      // 「已保存值非空、被用户清空」那种情况已在 onFinish 拦下并给了红字，走不到这里；
      // 剩下的空值只可能是「本来就为空」，此时不传才能让「只改启用开关」的保存不被连累。
      const issuerUrl = values.issuerUrl?.trim();
      if (issuerUrl) sso.issuer_url = issuerUrl;
      await updateIntegrationConfig({ sso });
      message.success(t('common.saveSuccess'));
      onChanged();
      onClose();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : dl('saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const onFinish = (values: SsoConfigFormValues) => {
    // 拦在第一位：清空不是有效操作，先纠正地址再谈别的。不拦的话 submit 会照常成功、
    // 界面弹「保存成功」而库里的地址纹丝不动 —— 正是这次要修的那个假象。
    // 输入框下方已经常驻红字（validateStatus/help），这里的 toast 是「点了没反应」的即时反馈。
    if (issuerUrlCleared) {
      message.error(dl('issuerUrlRequired'));
      return;
    }
    if (values.enabled && !status?.last_verified_at) {
      message.warning(dl('testFirst'));
      return;
    }
    if (!values.enabled && status?.enabled) {
      Modal.confirm({
        title: dl('disableConfirmTitle'),
        content: dl('disableConfirmDesc'),
        okText: t('common.confirm'),
        cancelText: t('common.cancel'),
        okButtonProps: { danger: true },
        onOk: () => submit(values),
      });
      return;
    }
    submit(values);
  };

  /**
   * 「测试连接」按钮入口：脏态先弹确认再探，否则直接探。
   *
   * 为什么要拦一下：探测只带已保存配置，输入框里未保存的新值不参与；用户改完不保存就点，
   * 探的是库里旧地址，报错却只说「SSO 连接失败」（2026-09-28 实测 16 次 5005）。
   * 把「将探哪个已保存地址」摆到确认框里，用户点确定前就能发现自己没保存。
   */
  const onTestConnection = () => {
    if (!issuerUrlDirty) {
      void runTestConnection();
      return;
    }
    // target 优先取后端三级解析出的真值（实际出站的是它，不是 issuer_url）；后端未部署该字段时
    // 回落到已保存的裸 issuer_url。两者都空时不硬塞空 target，改走无参文案。
    const target = effectiveProbeTarget || savedIssuerUrl;
    Modal.confirm({
      title: dl('unsavedConfirmTitle'),
      content: target
        ? dl('unsavedConfirmDesc', { target })
        : dl('unsavedConfirmDescNoTarget'),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      onOk: () => runTestConnection(),
    });
  };

  /** 真正发起一次探测（确认框的 onOk 复用它，两处只有这一个出站点）。 */
  const runTestConnection = async () => {
    setTesting(true);
    try {
      // 不带配置快照：测试连接只探测已存配置（issuer_url 取自 tenant_realm_map），
      // 传未保存的表单值等于绕过「保存」按钮产生隐式写入。
      await testConnection({ target: 'sso' });
      message.success(dl('testOk'));
      onChanged();
    } catch (e: unknown) {
      if (isSsoNotProvisioned(e)) {
        // 未开通 SSO 时后端返回 1002；给引导文案，别让用户看到「资源不存在」这类通用报错
        message.error(dl('realmNotOpened'));
      } else {
        // 失败文案回显后端**实际探了哪个地址**（errorDetails.target），否则界面显示 A、
        // 系统探 B 时用户无从得知。取的是后端回报的完整 probe_url，不是表单值 ——
        // 表单里可能是用户刚改、还没保存的地址，写它只会把人引向错误结论。
        // 后端未部署该字段时 errorDetails 缺失 → 回落到原来的中性文案。
        const baseMessage = e instanceof Error && e.message ? e.message : dl('testFailed');
        const probeTarget = readProbeTarget(e);
        message.error(
          probeTarget
            ? dl('testFailedWithTarget', { message: baseMessage, target: probeTarget })
            : baseMessage,
        );
      }
    } finally {
      setTesting(false);
    }
  };

  const onCopy = async (text: string) => {
    // 线上是 http 非安全上下文，clipboard API 不可用，copy-to-clipboard 内部自带 execCommand 降级
    if (await copyToClipboard(text)) message.success(t('common.copySuccess'));
    else message.error(t('common.copyFailed'));
  };

  // 映射关系固定不可编辑：三条基础字段落到 users 列，sub 作为唯一标识不落列
  const mappingRows: MappingRow[] = [
    ...LOGIN_FIELD_MAPPING.map((m) => ({
      key: m.key,
      fieldName: t(`tenantManagement.identity.${m.labelKey}`),
      providerField: m.key,
      localField: m.value,
      rule: dl('ruleBasic'),
    })),
    { key: 'sub', fieldName: dl('subLabel'), providerField: 'sub', localField: '', rule: dl('ruleFixed') },
  ];

  return (
    <Drawer
      title={dl('title')}
      open={open}
      onClose={onClose}
      width={560}
      footer={
        <Flex justify="space-between" align="center">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={() => form.submit()}>
            {dl('save')}
          </Button>
        </Flex>
      }
    >
      <Form<SsoConfigFormValues> form={form} layout="vertical" requiredMark={false} onFinish={onFinish}>
        {/* 分区一：基础配置 */}
        <SectionHeading icon={<SettingOutlined />} color={token.colorPrimary}>
          {dl('groupBasic')}
        </SectionHeading>

        {/* 启用开关行：浅色高亮卡，与同步抽屉的「自动同步」同构 */}
        <div
          style={{
            background: token.colorFillTertiary,
            borderRadius: token.borderRadiusLG,
            padding: `${token.paddingXS}px ${token.paddingSM}px`,
            marginTop: 16,
          }}
        >
          {/* Form.Item 只管 Switch：它把 value/onChange 注入唯一子节点，
              包住 Flex 会让注入落到 div 上，开关就只是自己变色、表单值不变。 */}
          <Flex justify="space-between" align="center">
            <Flex vertical gap={2}>
              <Text strong>{dl('enabled')}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {dl('enabledDesc')}
              </Text>
            </Flex>
            <Form.Item name="enabled" valuePropName="checked" noStyle>
              <Switch />
            </Form.Item>
          </Flex>
        </div>

        <Form.Item name="loginPolicy" label={dl('loginPolicy')} style={{ marginTop: 14 }}>
          <Select
            options={[
              {
                value: 'auto_create',
                label: (
                  <Flex vertical>
                    <span>{getLoginPolicyLabel(t, 'auto_create')}</span>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {dl('policyAutoCreateDesc')}
                    </Text>
                  </Flex>
                ),
              },
              {
                value: 'synced_only',
                label: (
                  <Flex vertical>
                    <span>{getLoginPolicyLabel(t, 'synced_only')}</span>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {dl('policySyncedOnlyDesc')}
                    </Text>
                  </Flex>
                ),
              },
            ]}
          />
        </Form.Item>

        <Divider style={{ margin: `${token.margin}px 0 ${token.marginLG}px` }} />

        {/* 分区二：高级配置（Realm 参数与字段映射；只有服务地址可改，其余只读） */}
        <Collapse
          ghost
          defaultActiveKey={['advanced']}
          expandIconPlacement="end"
          styles={{
            header: { padding: '8px 0' },
            body: { paddingTop: 2, paddingBottom: 0, paddingInline: 0 },
          }}
          items={[
            {
              key: 'advanced',
              label: (
                <SectionHeading icon={<ApiOutlined />} color={token.colorPrimary}>
                  {dl('groupAdvanced')}
                </SectionHeading>
              ),
              children: (
                <>
                  <Text type="secondary" style={{ display: 'block', fontSize: 13, marginBottom: 18 }}>
                    {dl('advancedHint')}
                  </Text>

                  {/* Realm 信息以服务端/运维侧数据为准，除服务地址外前端只读；每次打开抽屉重新拉取 */}
                  {realmState.kind === 'ready' ? (
                    <>
                      {/* 外层 Form.Item 只提供 label 布局（无 name，不注入表单值）；
                          name 必须挂在 Input 那一层 —— Form.Item 只把 value/onChange 注入
                          唯一子节点，直接写在 Space.Compact 上会注入到 div 上、输入框脱管
                          （与下方 Switch 同一个坑），故用 noStyle 的内层 Form.Item 兜住。 */}
                      {/* validateStatus/help 挂外层而非内层：内层的 name 那个 Form.Item 是
                          noStyle（只认领值、不渲染反馈区），单靠它报错用户什么都看不到。
                          这里直出「不能清空」的红字，与 onFinish 的拦截同一个判据。 */}
                      <Form.Item
                        label={dl('realmIssuerUrl')}
                        style={{ marginBottom: 8 }}
                        validateStatus={issuerUrlCleared ? 'error' : undefined}
                        help={issuerUrlCleared ? dl('issuerUrlRequired') : undefined}
                      >
                        <Space.Compact style={{ width: '100%' }}>
                          <Form.Item name="issuerUrl" noStyle>
                            <Input />
                          </Form.Item>
                          {/* 测试连接：后端出站探测 Keycloak 的 OIDC 发现端点，成功则刷新验证时间。
                              它只探测已存配置、不带表单快照 —— 未保存的地址不该被隐式写库；
                              脏态下会先弹确认（见 onTestConnection），不做隐式写入。 */}
                          <Button loading={testing} onClick={onTestConnection}>
                            {dl('testConn')}
                          </Button>
                        </Space.Compact>
                      </Form.Item>
                      {/* 未保存提示：紧贴输入框，与下面的「将探测」是两件事 —— 这条讲的是
                          「输入框和已保存值不一致」，那条讲的是「不一致时按钮会探哪个地址」。
                          清空时由上面的红字独占（不叠加），见 issuerUrlDirty 的定义。 */}
                      {issuerUrlDirty && !issuerUrlCleared && (
                        <Text type="warning" style={{ display: 'block', fontSize: 12, marginBottom: 8 }}>
                          {dl('unsavedChanges')}
                        </Text>
                      )}
                      {/* 探测目标提示：紧贴按钮，把「按钮到底会探哪个地址」写出来。
                          上面那个按钮有意不带表单快照（只探已存配置），所以输入框里可能是
                          用户刚改、还没保存的新值 —— 不写出来，用户会以为测的就是眼前这个地址。
                          优先显示后端回传的 effective_probe_target：它是三级解析（白名单反查 →
                          KEYCLOAK_SERVER_URL → issuer_url）后的**完整**发现端点 URL，与浏览器
                          视角的 issuer_url 不是一回事，直出真值才不至于「界面显示 A、系统探 B」。
                          老后端不返回该字段时回落到裸 issuer_url + 一句「OIDC 发现端点」补语义：
                          那时后端出站的是 `issuer_url.rstrip("/") + "/.well-known/openid-configuration"`。 */}
                      <Text type="secondary" style={{ display: 'block', fontSize: 12, marginBottom: 8 }}>
                        {effectiveProbeTarget
                          ? dl('probeTargetHintFull', { target: effectiveProbeTarget })
                          : savedIssuerUrl
                            ? dl('probeTargetHint', { target: savedIssuerUrl })
                            : dl('probeTargetUnset')}
                      </Text>
                      {/* 这行说明专防一种误会：用户改完地址、看到「测试通过」，就以为登录已经切到
                          新地址。实际登录链路读的是部署侧环境变量，租户反查只按 realm 列匹配。
                          且本字段（`issuer_url`）在探测里也已退居**末档兜底**——探测目标按
                          白名单反查 → KEYCLOAK_SERVER_URL → 本列三级解析，改它未必改变探测结果，
                          更不改登录。 */}
                      <Text type="secondary" style={{ display: 'block', fontSize: 12, marginBottom: 8 }}>
                        {dl('realmIssuerUrlHint')}
                      </Text>
                      {/* 未验证用 warning 色而非次要色，提示需先测试连接 */}
                      <Text
                        type={status?.last_verified_at ? 'success' : 'warning'}
                        style={{ display: 'block', fontSize: 12, marginBottom: 20 }}
                      >
                        {status?.last_verified_at
                          ? dl('lastVerified', { time: fmtDateTime(status.last_verified_at) })
                          : dl('notVerified')}
                      </Text>

                      <Row gutter={16}>
                        <Col span={12}>
                          <Form.Item label={dl('realmId')}>
                            <Input readOnly value={realmState.data.realm} />
                          </Form.Item>
                        </Col>
                        <Col span={12}>
                          <Form.Item label={dl('realmClientId')}>
                            <Input readOnly value={realmState.data.client_id || ''} />
                          </Form.Item>
                        </Col>
                      </Row>

                      <Row gutter={16}>
                        <Col span={12}>
                          <Form.Item label={dl('clientSecret')}>
                            <Input readOnly value="" placeholder={dl('clientSecretPlaceholder')} />
                          </Form.Item>
                        </Col>
                        <Col span={12}>
                          <Form.Item label={dl('realmRedirectUri')}>
                            <Space.Compact style={{ width: '100%' }}>
                              <Input readOnly value={realmState.data.redirect_uri || ''} />
                              <Button
                                icon={<CopyOutlined />}
                                aria-label={t('common.copy')}
                                onClick={() => onCopy(realmState.data.redirect_uri || '')}
                              />
                            </Space.Compact>
                          </Form.Item>
                        </Col>
                      </Row>

                      <Text
                        type={realmState.data.status === 1 ? 'success' : 'warning'}
                        style={{ display: 'block', fontSize: 12, marginBottom: 20 }}
                      >
                        {dl('realmStatus')}：{realmState.data.status === 1 ? dl('realmEnabled') : dl('realmDisabled')}
                      </Text>
                    </>
                  ) : (
                    // 未就绪时不铺空表单：整块退化成一行状态 + 测试连接（未开通时测试连接会给出开通引导）
                    <Flex justify="space-between" align="center" style={{ marginBottom: 20 }}>
                      {realmState.kind === 'loading' ? (
                        <Spin size="small" />
                      ) : (
                        <Text type={realmState.kind === 'error' ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
                          {realmState.kind === 'error' ? dl('realmLoadFailed') : dl('realmNotOpened')}
                        </Text>
                      )}
                      <Button loading={testing} onClick={onTestConnection}>
                        {dl('testConn')}
                      </Button>
                    </Flex>
                  )}

                  {/* 登录字段映射：映射关系固定、前端不可改，平铺成表以便与 OIDC 配置逐条对照 */}
                  <Table<MappingRow>
                    size="small"
                    rowKey="key"
                    pagination={false}
                    dataSource={mappingRows}
                    columns={[
                      {
                        title: dl('colFieldName'),
                        dataIndex: 'fieldName',
                        width: 110,
                        render: (v: string) => <Text style={{ fontSize: 13 }}>{v}</Text>,
                      },
                      {
                        title: dl('colFieldMapping'),
                        key: 'mapping',
                        render: (_: unknown, row: MappingRow) => (
                          // 三段各自定宽：企业侧字段长短不一（preferred_username / name），
                          // 直接拼成一行会让箭头和目标字段逐行错开
                          <span
                            style={{
                              display: 'grid',
                              gridTemplateColumns: '160px 24px 1fr',
                              fontSize: 13,
                            }}
                          >
                            <span>{row.providerField}</span>
                            <span>{row.localField ? '→' : ''}</span>
                            <span>{row.localField}</span>
                          </span>
                        ),
                      },
                      {
                        title: dl('colRule'),
                        dataIndex: 'rule',
                        width: 76,
                        align: 'right',
                        render: (v: string) => (
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {v}
                          </Text>
                        ),
                      },
                    ]}
                  />
                </>
              ),
            },
          ]}
        />
      </Form>
    </Drawer>
  );
}
