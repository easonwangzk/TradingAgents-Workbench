const KEY = 'tradingagents.studio.language';

const english = {
  '成功': 'Success', '时间': 'Time',
  '管理': 'Management', '管理内容': 'Management content', '概览': 'Overview', '账户与权限': 'Accounts and permissions',
  '用量': 'Usage', '审计': 'Audit', '刷新管理数据': 'Refresh management data', '账户': 'Account', '账户总数': 'Total accounts',
  '已启用': 'Enabled', '研究运行': 'Research runs', '运行总数': 'Total runs', '托管身份月度额度': 'Managed identity monthly allowance',
  '月度限额': 'Monthly limit', '已使用': 'Spent', '已预留': 'Reserved', '最近错误': 'Recent errors', '运行': 'Run',
  '错误代码': 'Error code', '暂无错误记录': 'No recent errors', '姓名或邮箱…': 'Name or email…', '搜索账户': 'Search accounts',
  '账户状态': 'Account status', '全部账户': 'All accounts', '研究': 'Research', '助手': 'Assistant', '操作': 'Action',
  '暂无匹配账户': 'No matching accounts', '邀请邮箱': 'Invitation email', '用量区间': 'Usage period', '账户筛选': 'Account filter',
  '7 天': '7 days', '30 天': '30 days', '90 天': '90 days', '账户用量': 'Account usage', '研究次数': 'Research runs',
  '研究失败': 'Failed research', '助手调用': 'Assistant calls', '平台费用 USD': 'Platform cost USD', '外部费用 USD': 'External cost USD',
  '最近活动': 'Last activity', '暂无用量记录': 'No usage records', '账户审计': 'Account audit', '操作者': 'Actor',
  '目标账户': 'Target account', '之前': 'Before', '之后': 'After', '结果': 'Result', '暂无审计记录': 'No audit records',
  '确认操作': 'Confirm action', '确认': 'Confirm', '覆盖起始': 'Coverage since', '有界读取已截断': 'Bounded read truncated',
  '有界读取未截断': 'Bounded read not truncated', '固定所有者': 'Fixed owner', '保存权限': 'Save permissions',
  '生成重置链接': 'Create recovery link', '列表版本': 'List revision', '需要刷新账户列表': 'Refresh accounts required',
  '正在读取管理数据…': 'Loading management data…', '正在保存管理操作…': 'Saving management action…',
  '账户列表已变更，请刷新后重新检查。': 'Account list changed. Refresh and review before saving.',
  '操作结果未确认，请刷新账户列表后核对。': 'Action outcome is unconfirmed. Refresh accounts to verify.',
  '管理数据暂不可用，请刷新重试。': 'Management data unavailable. Refresh to try again.',
  '管理操作无效，请检查输入。': 'Invalid management action. Check your input.',
  '确认禁用账户并撤销会话？': 'Disable this account and revoke its sessions?',
  '确认生成账户重置链接？': 'Create a recovery link for this account?', '确认生成邀请链接？': 'Create this invitation link?',
  '请输入有效邮箱。': 'Enter a valid email.', '打开安全链接': 'Open secure link', '是': 'Yes', '否': 'No',
  '行情': 'Market', '行情来源': 'Market source', '自动刷新': 'Auto refresh', '刷新行情': 'Refresh market',
  '自选证券': 'Watchlist', '添加证券代码': 'Add ticker', '添加证券': 'Add security', '删除证券': 'Remove security',
  '代码或名称…': 'Ticker or name…', '搜索自选证券': 'Search watchlist', '自选证券行情': 'Watchlist quotes',
  '证券': 'Security', '价格': 'Price', '涨跌 %': 'Change %', '删除': 'Remove',
  '自选列表为空': 'Your watchlist is empty', '暂无匹配的证券': 'No matching securities',
  '证券详情': 'Security detail', '返回自选列表': 'Back to watchlist',
  '开盘': 'Open', '最高': 'High', '最低': 'Low', '昨收': 'Previous close', '成交量': 'Volume', '交易所': 'Exchange',
  '价格区间': 'Price range', '图表类型': 'Chart type', '收盘线': 'Closing line', '蜡烛图': 'Candlesticks',
  '每日价格图': 'Daily price chart', '选择证券查看日线': 'Select a security for daily bars', '暂无日线数据': 'No daily bars available',
  '日线 · 非实时': 'Daily bars · not real time', '读取时间': 'Fetched', '行情时间': 'Quote as of',
  '正在读取行情…': 'Loading market data…', '正在保存自选列表…': 'Saving watchlist…', '图表暂时不可用。': 'Chart unavailable.',
  '自选列表最多 12 个证券。': 'A watchlist supports up to 12 securities.',
  '请输入有效且不重复的证券代码。': 'Enter valid, unique tickers.',
  '自选列表已在其他窗口更新，请重新打开行情后再编辑。': 'Watchlist changed in another window. Reopen Market before editing.',
  '行情请求达到限制，自动刷新已暂停。': 'Market request limit reached. Auto refresh paused.',
  '当前会话无权读取行情，请重新登录。': 'This session cannot access market data. Sign in again.',
  '行情暂时不可用。': 'Market data unavailable.', '行情响应无效。': 'Invalid market response.', '行情参数无效。': 'Invalid market parameters.',
  'RESEARCH STUDIO / 研究工作台': 'RESEARCH STUDIO',
  'RESEARCH STUDIO / 邮箱注册': 'RESEARCH STUDIO / EMAIL REGISTRATION',
  'RESEARCH STUDIO / 账户安全': 'RESEARCH STUDIO / ACCOUNT SECURITY',
  '研究工作台': 'Research studio', '工作空间': 'Workspace', '研究助手': 'Research assistant', '新建研究': 'New research',
  '研究任务': 'Research tasks', '工作空间导航': 'Workspace navigation', '研究任务列表': 'Research task list',
  '任务状态': 'Task status', '全部': 'All', '已中断': 'Interrupted', '返回研究任务': 'Back to research tasks',
  '对话状态': 'Conversation status', '搜索对话': 'Search conversations', '对话名称、内容…': 'Conversation title or content…',
  '暂无匹配的对话': 'No matching conversations', '暂不可用': 'Unavailable',
  '仅使用你的运行历史与 TradingAgents 产品信息': 'Uses only your run history and TradingAgents product information',
  '新建对话': 'New conversation', '已保存的对话': 'Saved conversations', '输入消息': 'Message',
  '询问你的运行历史或 TradingAgents…': 'Ask about your run history or TradingAgents…',
  'GPT-5.6 Luna · 会消耗模型额度': 'GPT-5.6 Luna · uses model allowance', '发送': 'Send',
  '返回工作台': 'Back to workspace', '选择已有对话，或新建一条开始。': 'Choose a conversation or start a new one.',
  '你': 'You', '重命名对话': 'Rename conversation', '删除对话': 'Delete conversation',
  '对话名称': 'Conversation name', '保存': 'Save', '取消': 'Cancel',
  '请输入 1 到 80 个字符的标题。': 'Enter a title between 1 and 80 characters.',
  '正在读取已保存的对话…': 'Loading saved conversations…', '正在读取对话…': 'Loading conversation…',
  '正在新建对话…': 'Creating conversation…', '研究助手正在整理回答…': 'Research Assistant is preparing a reply…',
  '正在使用 GPT-5.6 Luna 回答…': 'Asking GPT-5.6 Luna…', '回答已保存。': 'Reply saved.',
  '登录已失效，请重新登录。': 'Your session expired. Sign in again.',
  '对话数量或用量达到限制。': 'Conversation or usage limit reached.',
  '本月模型额度不足。': 'Monthly model allowance is insufficient.',
  '研究助手暂时不可用，请稍后重试。': 'Research Assistant is unavailable. Try again later.',
  '跳至报告工作区': 'Skip to report workspace', '运行记录': 'Run records',
  '01 / 市场': '01 / Market', '02 / 基本面': '02 / Fundamentals',
  '03 / 新闻': '03 / News', '04 / 情绪': '04 / Sentiment',
  '连接异常': 'Connection error', '记录或接口不存在，请刷新历史。': 'Run or endpoint not found. Refresh history.',
  '数据源与回退顺序': 'Data sources and fallback order', '6 类': '6 categories',
  '减少投资辩论轮数': 'Decrease investment debate rounds', '增加投资辩论轮数': 'Increase investment debate rounds',
  '减少风险辩论轮数': 'Decrease risk debate rounds', '增加风险辩论轮数': 'Increase risk debate rounds',
  '关闭': 'Close', '搜索运行事件…': 'Search run events…',
  '任务内容': 'Run content', '任务进度': 'Run progress',
  '独立视角，共同求证。': 'Independent perspectives. Shared conviction.', '证券研究工作台': 'Equity research studio',
  '市场': 'Market', '基本面': 'Fundamentals', '新闻': 'News', '情绪': 'Sentiment',
  '返回所选研究': 'Return to selected research', '配置': 'Configuration', '历史': 'History',
  '连接中': 'Connecting', '运行历史': 'Run history', '正在读取历史…': 'Loading history…',
  '研究参数': 'Research parameters', '正在读取配置…': 'Loading configuration…',
  '研究配置': 'Research configuration', '运行模式': 'Run mode', '研究分析': 'Research analysis',
  '连通测试': 'Connectivity test', '证券代码': 'Ticker', '分析日期': 'Analysis date',
  '资产类型': 'Asset type', '报告语言': 'Report language', '中文': 'Chinese',
  '分析师': 'Analysts', '投资辩论轮数': 'Investment debate rounds', '风险辩论轮数': 'Risk debate rounds',
  '当前模型': 'Current model', '开始研究': 'Start research', '开始测试': 'Start test',
  '正在提交…': 'Submitting…', '股票 · Stock': 'Stock', '基金 · ETF': 'ETF',
  '外汇 · 暂不支持': 'Forex · unavailable', '加密资产 · 暂不支持': 'Crypto · unavailable',
  '指数 · 暂不支持': 'Index · unavailable', '任务进度': 'Run progress',
  '尚未启动': 'Not started', '研究进程': 'Research flow', '数据连通': 'Data connectivity',
  '运行事件': 'Run events', '研究报告': 'Research reports', '报告章节': 'Report sections',
  '完整报告目录': 'Full report directory', '暂无研究报告': 'No research reports yet',
  '尚无已验证的任务结果': 'No verified run results yet', '最终决策': 'Final decision',
  '等待任务记录': 'Waiting for a run', '尚未选择任务': 'No run selected',
  '提供商连通状态': 'Provider connectivity', '尚无连通测试结果': 'No connectivity results yet',
  '未测试': 'Not tested', '状态': 'Status', '耗时': 'Duration', '诊断': 'Diagnostics',
  '详细运行事件': 'Detailed run events', '事件类型': 'Event type', '全部事件': 'All events',
  '阶段 / 工具': 'Phase / tool', '用量': 'Usage', '连通性': 'Connectivity',
  '结果': 'Result', '错误': 'Error', '搜索载荷': 'Search payload',
  '尚无运行事件': 'No run events yet', '研究摘要': 'Research summary',
  '模型与用量': 'Model and usage', '任务模型快照': 'Run model snapshot',
  '模型调用': 'Model calls', '工具调用': 'Tool calls', '输入 tokens': 'Input tokens',
  '输出 tokens': 'Output tokens', '输入': 'Input', '输出': 'Output',
  '研究团队': 'Research team', '数据覆盖': 'Data coverage', '数据来源': 'Data sources',
  '仅供研究 · 不执行交易': 'For research only · No trading', '等待连接': 'Waiting for connection',
  '停止': 'Stop', '重跑': 'Rerun', '重新登录或授权': 'Sign in again',
  '重新登录': 'Sign in again', '任务参数': 'Run parameters', '载入配置': 'Load configuration',
  '原始请求 JSON': 'Raw request JSON', '核对执行': 'Reconcile execution',
  '继续执行': 'Resume', '浅色': 'Light', '深色': 'Dark', '舒适': 'Comfortable',
  '紧凑': 'Compact', '外观': 'Appearance', '主题': 'Theme',
  '工作台密度': 'Workspace density', '配色方案': 'Color palette',
  '登录背景网格': 'Login background grid', '减少动态效果': 'Reduce motion',
  '黑白': 'Monochrome', '冰蓝': 'Ice blue', '松绿': 'Pine green',
  '珊瑚': 'Coral', '琥珀': 'Amber', '界面语言': 'Interface language',
  '跟随浏览器': 'Follow browser', '英语': 'English', '关闭': 'Close',
  '外观设置': 'Appearance settings', '切换主题': 'Toggle theme',
  '切换浅色主题': 'Switch to light theme', '切换深色主题': 'Switch to dark theme',
  '收起侧栏': 'Collapse sidebar', '展开侧栏': 'Expand sidebar',
  '关闭侧栏': 'Close sidebar', '刷新配置、历史与任务': 'Refresh configuration, history and runs',
  '工作台首页': 'Workspace home', '侧栏视图': 'Sidebar views',
  '研究配置与运行历史': 'Research configuration and history',
  '搜索运行历史': 'Search run history', '代码、日期、状态…': 'Ticker, date, status…',
  '关闭研究参数': 'Close research parameters', '恢复默认配置': 'Restore defaults',
  '关闭外观设置': 'Close appearance settings',
  'Azure 与 Foundry': 'Azure and Foundry', '资源配置': 'Resource profiles',
  '管理 Azure 资源': 'Manage Azure resources', '关闭资源配置': 'Close resource settings',
  '重新授权': 'Authorize again', '新建配置': 'New profile', '浏览 Azure': 'Browse Azure',
  '订阅': 'Subscription', '资源组': 'Resource group', '配置名称': 'Profile name',
  '研究 Job 资源 ID': 'Research Job resource ID',
  '日志工作区资源 ID': 'Log workspace resource ID',
  'Foundry 账户资源 ID': 'Foundry account resource ID',
  '模型部署': 'Model deployment', '验证并保存': 'Validate and save',
  '删除配置': 'Delete profile', '确认删除配置': 'Confirm profile deletion',
  '提供商密钥': 'Provider keys', '停用 Alpha Vantage 绑定': 'Disable Alpha Vantage binding',
  '停用 FRED 绑定': 'Disable FRED binding', '停用': 'Disable',
  '保存密钥': 'Save keys', '暂无资源配置': 'No resource profiles',
  'Azure 更新结果待核对': 'Azure update needs confirmation',
  '按需初始化': 'On-demand initialization', '订阅 ID': 'Subscription ID',
  '区域': 'Region', '资源前缀': 'Resource prefix',
  '已有 Foundry 账户资源 ID': 'Existing Foundry account resource ID',
  '预览资源与授权': 'Preview resources and permissions',
  '初始化记录': 'Initialization history', '确认短语': 'Confirmation phrase',
  '确认此预览': 'Confirm this preview', '继续初始化': 'Resume initialization',
  '暂停': 'Pause', '刷新初始化记录': 'Refresh initialization history',
  '暂无初始化记录': 'No initialization records', '初始化完成': 'Initialization complete',
  '新建资源组: ': 'New resource group: ', '使用资源组: ': 'Use resource group: ',
  '镜像仓库': 'Container registry', '日志工作区': 'Log workspace',
  '运行身份': 'Runtime identity', '容器环境': 'Container environment',
  '镜像读取授权': 'Registry pull permission', 'Foundry 授权': 'Foundry permission',
  '构建镜像': 'Build image', '研究 Job': 'Research Job',
  'Agent 过程': 'Agent process', '轮询快照': 'Polling snapshot',
  '过程回放': 'Process replay', '过程回放播放': 'Play process replay',
  '过程回放暂停': 'Pause process replay', '返回实时': 'Return to live',
  '当前': 'Current', '回放速度': 'Replay speed', '过程回放进度': 'Replay progress',
  'Agent 过程图': 'Agent process graph', '等待过程数据': 'Waiting for process data',
  '尚未收到事件。': 'No events received yet.', '该节点尚无已观察到的事件。': 'No observed events for this node yet.',
  '实时': 'Live', '回放': 'Replay', '运行快照': 'Run snapshot',
  '播放': 'Play', '暂停': 'Pause', '访问': 'Visits', '工具': 'Tools',
  '未观察': 'Not observed', '已观察': 'Observed', '进行中': 'In progress', '失败': 'Failed',
  '重连中': 'Reconnecting', '混合事件 / 含日志补齐': 'Mixed events / logs backfilled',
  '事件更新 / 实时': 'Live event updates', '延迟日志': 'Delayed logs',
  '未选择分析师': 'No analysts selected',
  '01 · 多维分析': '01 · Multidimensional analysis', '02 · 投资辩论': '02 · Investment debate',
  '03 · 交易方案': '03 · Trading plan', '04 · 风险决策': '04 · Risk decision',
  '分析师 / 工具循环': 'Analyst / tool loop', '多空辩论循环': 'Bull / bear debate loop',
  '三方风控循环': 'Three-way risk loop',
  '市场分析师': 'Market analyst', '社交情绪分析师': 'Social sentiment analyst',
  '新闻分析师': 'News analyst', '基本面分析师': 'Fundamental analyst',
  '看涨研究员': 'Bull researcher', '看跌研究员': 'Bear researcher',
  '研究主管': 'Research manager', '交易员': 'Trader',
  '激进风险分析师': 'Aggressive risk analyst', '保守风险分析师': 'Conservative risk analyst',
  '中性风险分析师': 'Neutral risk analyst', '投资组合经理': 'Portfolio manager',
  '全局搜索': 'Global search', '搜索任务与报告': 'Search runs and reports',
  '搜索任务与报告 (Ctrl+K)': 'Search runs and reports (Ctrl+K)',
  '搜索证券、任务或报告…': 'Search tickers, runs or reports…',
  '关闭搜索': 'Close search', '匹配任务与报告': 'Matching runs and reports',
  '没有匹配的任务或报告': 'No matching runs or reports',
  '请求内容无效，请检查后重试。': 'Invalid request. Check your entries and try again.',
  '登录状态已失效，请重新登录。': 'Your session expired. Please sign in again.',
  '当前操作不被允许。': 'This action is not permitted.',
  '当前状态已变化，请刷新后重试。': 'The state changed. Refresh and try again.',
  '请求过于频繁，请稍后重试。': 'Too many requests. Try again later.',
  '输入未通过校验，请检查后重试。': 'Some entries are invalid. Check and try again.',
  '服务暂不可用，请稍后再试。': 'Service unavailable. Try again later.',
  '请输入有效邮箱地址。': 'Enter a valid email address.',
  '请输入 6 位验证码。': 'Enter a 6-digit verification code.',
  '密码长度需为 12-128 个字符。': 'Password must be 12 to 128 characters.',
  '两次输入的密码不一致。': 'Passwords do not match.',
  '如该邮箱可注册，验证码将发送至邮箱，请查收。': 'If this email can register, check your inbox for a verification code.',
  '验证码发送失败，请稍后重试。': 'Could not send the code. Try again later.',
  '验证码错误或已失效，请检查或重新获取。': 'The code is incorrect or expired. Check it or request a new one.',
  '注册失败，请稍后重试。': 'Registration failed. Try again later.',
  '链接无效或已失效，请重新获取。': 'This link is invalid or expired. Request a new one.',
  '请求失败，请稍后重试。': 'Request failed. Try again later.',
  '账户与安全': 'Account and security', '关闭账户面板': 'Close account panel',
  '管理员 · 可使用托管身份': 'Administrator · Managed identity available',
  '受邀用户 · 仅可使用 BYOK': 'Invited user · BYOK only',
  'UTC 重置：': 'UTC reset:', '复制链接': 'Copy link', '撤销邀请': 'Revoke invitation',
  '重置链接': 'Reset link', '禁用': 'Disable', '已禁用': 'Disabled', '管理员': 'Administrator',
  '本月模型预算': 'Monthly model budget', '上限': 'Limit', '保守扣减': 'Conservative deduction',
  '预留': 'Reserved', '可用': 'Available',
  '仅本工作台托管身份模型调用，非 Azure 账单。按长上下文单价保守扣减，实际账单可能更低；不含 BYOK、数据源和基础设施费用。': 'Managed-identity model calls in this workspace only, not an Azure bill. Conservative long-context pricing; actual charges may be lower. Excludes BYOK, data sources and infrastructure.',
  '修改密码': 'Change password', '当前密码': 'Current password', '新密码': 'New password',
  '更新密码': 'Update password', '邀请新用户': 'Invite user', '邮箱': 'Email',
  '生成邀请链接': 'Create invitation link', '兼容接口白名单': 'Compatible API allowlist',
  '每行一个 HTTPS Origin': 'One HTTPS origin per line', '保存白名单': 'Save allowlist',
  '用户': 'Users', '退出登录': 'Sign out',
  '模型与凭据': 'Models and credentials', '未载入': 'Not loaded',
  '运行凭据模式': 'Credential mode', '托管身份': 'Managed identity',
  '模型提供商': 'Model provider', '快速模型': 'Quick model', '深度模型': 'Deep model',
  '自定义快速模型': 'Custom quick model', '自定义深度模型': 'Custom deep model',
  '接口 Endpoint': 'API endpoint', 'Azure API 版本': 'Azure API version',
  '模型 API Key': 'Model API key', '删除模型密钥': 'Delete model key',
  '删除 Alpha Key': 'Delete Alpha key', '删除 FRED Key': 'Delete FRED key',
  '保存设置': 'Save settings', '自定义': 'Custom',
  '已完成': 'Completed', '已停止': 'Stopped', '已失败': 'Failed',
  '正在运行': 'Running', '正在启动': 'Starting', '正在恢复': 'Recovering',
  '停止处理中': 'Stopping', '执行异常': 'Execution error',
  '历史暂不可用': 'History unavailable', '没有匹配的运行记录': 'No matching runs',
  '暂无运行记录': 'No runs yet', '尚无执行名称': 'No execution name',
  '全部章节': 'All sections', '未配置': 'Not configured', '已配置': 'Configured', '已连接': 'Connected',
  '缺少密钥': 'Missing key', '已跳过': 'Skipped', '连接失败': 'Connection failed',
  '不可用': 'Unavailable', '未知': 'Unknown', '已配置 · 尚未验证': 'Configured · not verified',
  '已记录': 'Recorded', '研究已完成': 'Research complete',
  '正在启动云端任务': 'Starting cloud run', '等待日志到达与结果校验': 'Waiting for logs and verification',
  '检查提供商连通性': 'Checking provider connectivity', '执行研究分析': 'Running research analysis',
  '初始化研究分析': 'Initializing research', '市场分析': 'Market analysis',
  '社交情绪分析': 'Social sentiment analysis', '新闻分析': 'News analysis',
  '基本面分析': 'Fundamental analysis', '看涨研究': 'Bull research',
  '看跌研究': 'Bear research', '投资辩论': 'Investment debate',
  '交易决策': 'Trading decision', '激进风险评估': 'Aggressive risk assessment',
  '保守风险评估': 'Conservative risk assessment', '中性风险评估': 'Neutral risk assessment',
  '投资组合管理': 'Portfolio management', '数据准备': 'Data preparation',
  '多维分析': 'Multidimensional analysis', '风险评估': 'Risk assessment',
  '报告生成': 'Report generation', '完整事件 JSON': 'Full event JSON',
  '没有匹配的运行事件': 'No matching run events',
  '个提供商': 'providers', '字符': 'characters', '已检查': 'Checked',
  '已接收结果事件': 'Result event received', '阶段': 'Phase', '报告': 'Report',
  '连通': 'Connectivity', '模型': 'Model', '新增': 'Added',
  '尚无已记录模型调用': 'No recorded model calls',
  '更新': 'Updated', '本地会话 · 自动同步': 'Local session · Auto sync',
  '邮箱会话 · 自动同步': 'Email session · Auto sync',
  'Entra 会话 · 自动同步': 'Entra session · Auto sync',
  '页面隐藏 · 同步暂停': 'Page hidden · Sync paused',
  '报告生成 · 等待结果校验': 'Generating report · Awaiting verification',
  '报告收集中 · 等待日志到达与结果校验': 'Collecting reports · Awaiting logs and verification',
  '调用研究工具': 'Calling research tools',
  '容器已结束，等待日志到达并校验完整结果。': 'Execution ended. Waiting for logs and complete result verification.',
  '任务进行中，尚未收到报告。': 'Run in progress. No report received yet.',
  '任务失败，尚无可用报告。': 'Run failed. No report is available.',
  '任务已停止，尚未生成报告。': 'Run stopped before producing a report.',
  '运行中': 'Running', '正在准备': 'Preparing', '已准备': 'Prepared',
  '待核对': 'Needs reconciliation', '报告收集中': 'Collecting reports',
  '行情数据': 'Market data', '技术指标': 'Technical indicators',
  '基本面数据': 'Fundamental data', '宏观数据': 'Macro data',
  '预测市场': 'Prediction markets',
  '社交': 'Social', '市场分析': 'Market analysis',
  '投资计划': 'Investment plan', '交易员方案': 'Trader plan',
  '多方观点': 'Bull view', '空方观点': 'Bear view', '研究裁决': 'Research verdict',
  '激进风险观点': 'Aggressive risk view', '保守风险观点': 'Conservative risk view',
  '中性风险观点': 'Neutral risk view', '风险裁决': 'Risk verdict',
  '运行 ID': 'Run ID', '运行模式': 'Run mode', '模型快照': 'Model snapshot',
  '辩论 / 风险': 'Debate / risk', '任务模型快照': 'Run model snapshot',
  '提交当前研究配置': 'Submit current research configuration',
  '以当前配置测试提供商连接': 'Test providers using current configuration',
  '已有任务运行，等待结束或停止后再提交': 'A run is active. Wait for completion or stop it first.',
  '研究表单中的资产或数据源与已保存设置不一致，请先保存设置。': 'Research assets or data sources differ from saved settings. Save settings first.',
  '部署或会话尚未就绪': 'Deployment or session is not ready',
  '设置就绪': 'Settings ready', '设置未完成': 'Settings incomplete',
  '部署就绪': 'Deployment ready', '部署未就绪': 'Deployment not ready',
  '未选择资源': 'No resource selected', '设置已保存。': 'Settings saved.',
  '正在保存设置…': 'Saving settings…', '设置保存失败，请稍后重试。': 'Could not save settings. Try again later.',
  '无需 API Key': 'No API key required', '未保存': 'Not saved',
  '已配置密钥': 'Key configured', '待配置密钥': 'Key needed',
  '模型密钥将于下次保存时删除。': 'Model key will be deleted when you next save.',
  'Alpha Vantage 密钥将于下次保存时删除。': 'Alpha Vantage key will be deleted when you next save.',
  'FRED 密钥将于下次保存时删除。': 'FRED key will be deleted when you next save.',
  '欢迎回来': 'Welcome back', '使用邮箱和密码登录研究工作台': 'Sign in to the research studio with your email and password',
  '邮箱地址': 'Email address', '密码': 'Password', '登录工作台': 'Sign in',
  '注册账户': 'Create account', '重置密码': 'Reset password',
  '使用邮箱登录，或通过验证码创建账户。': 'Sign in with email or create an account with a verification code.',
  '创建账户': 'Create account', '验证邮箱': 'Verify email',
  '验证码将发送到你的邮箱。': 'A verification code will be sent to your email.',
  '邮箱注册暂不可用，请稍后再试。': 'Email registration is unavailable. Please try again later.',
  '6 位验证码': '6-digit verification code', '发送验证码': 'Send code',
  '显示密码': 'Show password', '隐藏密码': 'Hide password',
  '大写锁定已开启': 'Caps Lock is on', 'TradingAgents 登录': 'TradingAgents sign in',
  '行情研究插画': 'Market research illustration', '邮箱注册': 'Email registration',
  '返回登录': 'Back to sign in', '确认密码': 'Confirm password',
  '兑换邀请': 'Redeem invitation', '账户安全': 'Account security',
  '请使用管理员提供的专属链接继续': 'Use the personal link provided by your administrator to continue',
  '正在验证链接…': 'Verifying link…', '新密码': 'New password', '提交': 'Submit',
  '邀请链接已载入：': 'Invitation link loaded: ', '重置链接已载入：': 'Reset link loaded: ',
  '后重发': 'until resend', '项结果 · 历史任务与已加载报告': 'results · history and loaded reports',
  '新增': 'Added', '尚无已记录模型调用': 'No recorded model calls',
  '新闻数据': 'News data',
  'ETF 基金资料使用 Yahoo Finance 原生接口': 'ETF fund data uses the native Yahoo Finance API',
  '账户信息加载失败。': 'Could not load account information.',
  '无法撤销邀请。': 'Could not revoke invitation.', '无法生成重置链接。': 'Could not create reset link.',
  '无法禁用用户。': 'Could not disable user.', '密码更新失败。': 'Could not update password.',
  '无法生成邀请链接。': 'Could not create invitation link.',
  '白名单保存失败。': 'Could not save allowlist.', '退出失败。': 'Could not sign out.',
};

const bindings = new WeakMap();
const attributes = new WeakMap();
let active = 'zh';
let preference = 'auto';
const subscribers = new Set();
export const locale = () => active === 'zh' ? 'zh-CN' : 'en-US';
export const translate = value => active === 'en' && typeof value === 'string' ? english[value] || value : value;
export const formatNumber = value => Number(value).toLocaleString(locale());
export const formatDate = (value, options) => new Date(value).toLocaleString(locale(), options);
export const onLocaleChange = callback => { subscribers.add(callback); return () => subscribers.delete(callback); };

export function bindLocale(doc, selector) {
  for (const root of doc.querySelectorAll(selector)) {
    const walker = doc.createTreeWalker(root, doc.defaultView.NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const text = walker.currentNode;
      if (text.parentElement.closest('script, style, pre, code, textarea, input, option[data-user-value], #report-reader, #event-list, #request-json, #account-email, #account-name, #user-list, #profile-select')) continue;
      const source = text.nodeValue.trim();
      if (Object.hasOwn(english, source) && !bindings.has(text)) bindings.set(text, { zh: text.nodeValue, en: text.nodeValue.replace(source, english[source]), current: text.nodeValue });
    }
    for (const element of [root, ...root.querySelectorAll('[title], [aria-label], [placeholder]')]) {
      for (const attr of ['title', 'aria-label', 'placeholder']) {
        const source = element.getAttribute(attr);
        if (source && Object.hasOwn(english, source)) {
          const originals = attributes.get(element) || {};
          if (!originals[attr]) { originals[attr] = { zh: source, en: english[source], current: source }; attributes.set(element, originals); }
        }
      }
    }
  }
  applyBindings(doc);
}

function applyBindings(doc) {
  for (const root of doc.querySelectorAll('[data-locale-root]')) {
    const walker = doc.createTreeWalker(root, doc.defaultView.NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const entry = bindings.get(walker.currentNode);
      if (entry) {
        if (walker.currentNode.nodeValue !== entry.current) { bindings.delete(walker.currentNode); continue; }
        walker.currentNode.nodeValue = entry[active];
        entry.current = walker.currentNode.nodeValue;
      }
    }
    for (const element of [root, ...root.querySelectorAll('[title], [aria-label], [placeholder]')]) {
      for (const attr of ['title', 'aria-label', 'placeholder']) {
        const entry = attributes.get(element)?.[attr];
        if (entry) {
          if (element.getAttribute(attr) !== entry.current) { delete attributes.get(element)[attr]; continue; }
          element.setAttribute(attr, entry[active]);
          entry.current = entry[active];
        }
      }
    }
  }
  doc.documentElement.lang = active === 'zh' ? 'zh-CN' : 'en';
  if (doc.documentElement.dataset.auth) {
    const route = doc.documentElement.dataset.authRoute || doc.documentElement.dataset.auth;
    const title = { register: '注册账户', reset: '重置密码', redeem: '兑换邀请' }[route];
    doc.title = title ? `${translate(title)} · TradingAgents` : active === 'zh' ? 'TradingAgents' : 'Sign in · TradingAgents';
  }
  else doc.title = active === 'zh' ? 'TradingAgents · 研究工作台' : 'TradingAgents · Research studio';
}

export function mountLocale(doc) {
  let saved = 'auto';
  try { saved = doc.defaultView.localStorage.getItem(KEY) || 'auto'; } catch {}
  preference = ['auto', 'zh', 'en'].includes(saved) ? saved : 'auto';
  const browser = /^zh(?:-|$)/i.test(doc.defaultView.navigator.language || '') ? 'zh' : 'en';
  active = preference === 'auto' ? browser : preference;
  doc.body.dataset.localeRoot = '';
  bindLocale(doc, 'body');
  return {
    get preference() { return preference; },
    translate,
    set(value) {
      if (!['auto', 'zh', 'en'].includes(value)) return;
      preference = value;
      active = value === 'auto' ? browser : value;
      try { doc.defaultView.localStorage.setItem(KEY, value); } catch {}
      applyBindings(doc);
      subscribers.forEach(callback => callback());
    },
    refresh() { applyBindings(doc); },
  };
}