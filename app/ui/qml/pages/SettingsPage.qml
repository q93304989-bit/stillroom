import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

/* 设置页：密钥与接口 / 图床 / LLM / 网络模式 / 主题 / 生成默认值 / 存储。
   密钥写进 .env（唯一入口），偏好写进 settings.json——两者都由 Python 侧负责落盘。 */
Item {
    id: page
    objectName: "settingsPage"

    readonly property var bridge: settingsBridge
    property var values: ({})
    property var versionList: []

    /* 只读探针：待决定建议那块实际画出来的宽度（隐藏时按 0 报）。
       给界面测试读——从 Python 侧 findChild 会被 shiboken 的包装缓存坑到。 */
    readonly property real pendingBoxWidth: pendingBox.visible ? pendingBox.width : 0

    /* 只读探针：设置页「找参考与上下文」卡片真的建出来了没有，以及两个当前值。 */
    readonly property bool contextCardReady:
        contextModeCombo.count > 0 && contextThresholdCombo.count > 0
        && searchProviderCombo.count > 0
    readonly property int contextModeIndex: contextModeCombo.currentIndex
    readonly property int contextThresholdIndex: contextThresholdCombo.currentIndex
    readonly property int searchProviderIndex: searchProviderCombo.currentIndex
    readonly property bool searchKeyConfigured: values.search_key_set === true
    readonly property bool webImagesAcknowledged: settingsBridge.webImagesAcknowledged
    /* 只读探针：免责声明确认框是不是开着（界面测试用它验证「首次开启会弹」）。 */
    readonly property bool webImagesDialogOpen: webImagesDialog.opened

    /* 打开免责声明确认框。勾选开关会自动走到这里；测试也从这里调。 */
    function askWebImagesConsent() {
        webImagesDialog.open();
    }

    function statusLabel(status) {
        return {
            "proposed": "待决定",
            "active": "生效中",
            "superseded": "已取代",
            "rejected": "已拒绝",
            "discarded": "已过期"
        }[status] || status;
    }

    function reloadVersions() {
        versionList = selfUpdateBridge.versions();
    }

    readonly property var networkLabels: ["自动（直连优先，失败换代理）", "仅直连", "仅系统代理"]
    readonly property var networkCodes: ["auto", "direct", "proxy"]
    readonly property var themeLabels: ["跟随系统", "浅色", "深色"]
    readonly property var themeCodes: ["system", "light", "dark"]
    readonly property var imageModels: ["agnes-image-2.5-flash", "agnes-image-2.1-flash"]
    readonly property var imageSizes: [
        "1024x768", "512x512", "768x1024", "1024x1024",
        "1280x720", "720x1280", "1920x1080"
    ]
    readonly property var videoModels: ["agnes-video-2.5-flash", "agnes-video-2.5", "agnes-video-v2.0"]
    readonly property var videoSeconds: ["4", "5", "8", "10", "12"]
    readonly property var videoAspects: ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
    readonly property var contextModeLabels: ["草稿模式（先出上下文再生成）", "自动模式（直接生成）"]
    readonly property var contextModeCodes: ["draft", "auto"]
    readonly property var refThresholds: ["0", "1", "2", "3", "4", "5"]
    readonly property var searchProviderLabels: [
        "tavily（默认）", "bocha（博查）", "serper", "custom（自建端点）", "off（关掉联网）"
    ]
    readonly property var searchProviderCodes: ["tavily", "bocha", "serper", "custom", "off"]
    readonly property string webImagesDisclaimer: settingsBridge.webImagesDisclaimer

    function reload() {
        values = bridge.currentValues();
        baseUrlField.text = values.agnes_base_url || "";
        apiKeyField.text = "";
        apiKeyField.placeholderText = values.agnes_key_set ? "已配置（留空则不修改）" : "sk-...";
        githubRepoField.text = values.github_repo || "";
        githubTokenField.text = "";
        githubTokenField.placeholderText = values.github_token_set ? "已配置（留空则不修改）" : "ghp_...";
        seeTokenField.text = "";
        seeTokenField.placeholderText = values.see_token_set ? "已配置（留空则不修改）" : "S.E.E API Key";
        llmBaseField.text = values.llm_base_url || "";
        llmModelField.text = values.llm_model || "";
        llmKeyField.text = "";
        llmKeyField.placeholderText = values.llm_key_set ? "已配置（留空则不修改）" : "sk-...";
        networkCombo.currentIndex = Math.max(0, networkCodes.indexOf(values.network_mode || "auto"));
        themeCombo.currentIndex = Math.max(0, themeCodes.indexOf(values.theme || "system"));
        imgModelCombo.currentIndex = Math.max(0, imageModels.indexOf(values.img_model || ""));
        imgSizeCombo.currentIndex = Math.max(0, imageSizes.indexOf(values.img_size || ""));
        vidModelCombo.currentIndex = Math.max(0, videoModels.indexOf(values.vid_model || ""));
        vidSecondsCombo.currentIndex = Math.max(0, videoSeconds.indexOf(values.vid_seconds || ""));
        vidAspectCombo.currentIndex = Math.max(0, videoAspects.indexOf(values.vid_aspect || ""));
        contextModeCombo.currentIndex =
            Math.max(0, contextModeCodes.indexOf(values.context_mode || "draft"));
        contextThresholdCombo.currentIndex = Math.max(0, Math.min(5, values.min_local_refs));
        var sources = values.context_sources || {};
        historySourceBox.checked = sources["history"] === true;
        knowledgeSourceBox.checked = sources["knowledge"] === true;
        webSourceBox.checked = sources["web"] === true;
        webImagesSourceBox.checked = sources["web_images"] === true;
        searchProviderCombo.currentIndex =
            Math.max(0, searchProviderCodes.indexOf(values.search_provider || "tavily"));
        searchKeyField.text = "";
        searchKeyField.placeholderText =
            values.search_key_set ? "已配置（留空则不修改）" : "SEARCH_API_KEY";
        searchBaseField.text = values.search_base_url || "";
    }

    ScrollView {
        id: scroll
        objectName: "settingsScroll"          // 截图工具靠它滚到自更新卡片
        anchors.fill: parent
        contentWidth: availableWidth
        ScrollBar.vertical.policy: ScrollBar.AsNeeded

        Item {
            width: scroll.availableWidth - 40
            implicitHeight: content.implicitHeight + 40

            ColumnLayout {
                id: content
                x: 20
                y: 20
                width: parent.width
                spacing: 16

                // ---------------- 密钥与接口 ----------------
                AppCard {
                    title: "密钥与接口"
                    Text {
                        width: parent.width
                        text: "密钥只写进程序目录的 .env，不会出现在 settings.json 或历史记录里。"
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "接口地址"; color: theme.textSub; font.pixelSize: 12 }
                            AppField {
                                id: baseUrlField
                                Layout.fillWidth: true
                                placeholderText: "https://apihub.agnes-ai.com/v1"
                            }
                        }
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "API Key"; color: theme.textSub; font.pixelSize: 12 }
                            AppField {
                                id: apiKeyField
                                Layout.fillWidth: true
                                echoMode: TextInput.Password
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        AppButton {
                            text: "保存"
                            primary: true
                            onClicked: bridge.saveAgnes({
                                "base_url": baseUrlField.text,
                                "api_key": apiKeyField.text
                            })
                        }
                        AppButton {
                            text: "测试连接"
                            onClicked: bridge.testConnection()
                        }
                        Text {
                            Layout.fillWidth: true
                            text: "当前站点：" + (values.site || "-") + " · 视频查询：" + (values.video_query_url || "-")
                            color: theme.textDisabled
                            font.pixelSize: 11
                            elide: Text.ElideMiddle
                        }
                    }
                }

                // ---------------- 图床 ----------------
                AppCard {
                    title: "图床（视频参考图需要公网直链）"
                    Text {
                        width: parent.width
                        text: "推荐免费的 GitHub 方案：新建一个公开仓库，再生成一个有 public_repo 权限的 token。"
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "仓库（用户名/仓库名）"; color: theme.textSub; font.pixelSize: 12 }
                            AppField { id: githubRepoField; Layout.fillWidth: true; placeholderText: "your-name/my-images" }
                        }
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "GitHub Token"; color: theme.textSub; font.pixelSize: 12 }
                            AppField { id: githubTokenField; Layout.fillWidth: true; echoMode: TextInput.Password }
                        }
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "S.E.E API Key（可选）"; color: theme.textSub; font.pixelSize: 12 }
                            AppField { id: seeTokenField; Layout.fillWidth: true; echoMode: TextInput.Password }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        AppButton {
                            text: "保存图床设置"
                            primary: true
                            onClicked: bridge.saveHosting({
                                "github_repo": githubRepoField.text,
                                "github_token": githubTokenField.text,
                                "see_token": seeTokenField.text
                            })
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                // ---------------- LLM ----------------
                AppCard {
                    title: "LLM（可选，OpenAI 兼容协议）"
                    Text {
                        width: parent.width
                        text: "任何兼容 /chat/completions 的服务都能用；填了换模型只改这里。未配置不影响主功能。"
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "接口地址"; color: theme.textSub; font.pixelSize: 12 }
                            AppField { id: llmBaseField; Layout.fillWidth: true; placeholderText: "https://api.deepseek.com/v1" }
                        }
                        ColumnLayout {
                            spacing: 6
                            Layout.preferredWidth: 200
                            Text { text: "模型"; color: theme.textSub; font.pixelSize: 12 }
                            AppField { id: llmModelField; Layout.fillWidth: true; placeholderText: "deepseek-chat" }
                        }
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "API Key"; color: theme.textSub; font.pixelSize: 12 }
                            AppField { id: llmKeyField; Layout.fillWidth: true; echoMode: TextInput.Password }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        AppButton {
                            text: "保存 LLM 设置"
                            primary: true
                            onClicked: bridge.saveLlm({
                                "llm_base_url": llmBaseField.text,
                                "llm_model": llmModelField.text,
                                "llm_api_key": llmKeyField.text
                            })
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                // ---------------- 网络与主题 ----------------
                AppCard {
                    title: "网络与外观"
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 24
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "网络模式"; color: theme.textSub; font.pixelSize: 12 }
                            RowLayout {
                                spacing: 8
                                AppCombo {
                                    id: networkCombo
                                    Layout.preferredWidth: 260
                                    model: page.networkLabels
                                }
                                AppButton {
                                    text: "应用"
                                    onClicked: bridge.saveNetworkMode(page.networkCodes[networkCombo.currentIndex])
                                }
                            }
                        }
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "主题"; color: theme.textSub; font.pixelSize: 12 }
                            RowLayout {
                                spacing: 8
                                AppCombo {
                                    id: themeCombo
                                    Layout.preferredWidth: 160
                                    model: page.themeLabels
                                }
                                AppButton {
                                    text: "应用"
                                    onClicked: bridge.saveTheme(page.themeCodes[themeCombo.currentIndex])
                                }
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                // ---------------- 生成默认值 ----------------
                AppCard {
                    title: "生成默认值（留空 = 用列表第一项）"
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "图片模型"; color: theme.textSub; font.pixelSize: 12 }
                            AppCombo { id: imgModelCombo; Layout.fillWidth: true; model: page.imageModels }
                        }
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "图片尺寸"; color: theme.textSub; font.pixelSize: 12 }
                            AppCombo { id: imgSizeCombo; Layout.fillWidth: true; model: page.imageSizes }
                        }
                        ColumnLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Text { text: "视频模型"; color: theme.textSub; font.pixelSize: 12 }
                            AppCombo { id: vidModelCombo; Layout.fillWidth: true; model: page.videoModels }
                        }
                        ColumnLayout {
                            spacing: 6
                            Layout.preferredWidth: 110
                            Text { text: "时长"; color: theme.textSub; font.pixelSize: 12 }
                            AppCombo { id: vidSecondsCombo; Layout.fillWidth: true; model: page.videoSeconds }
                        }
                        ColumnLayout {
                            spacing: 6
                            Layout.preferredWidth: 120
                            Text { text: "画幅"; color: theme.textSub; font.pixelSize: 12 }
                            AppCombo { id: vidAspectCombo; Layout.fillWidth: true; model: page.videoAspects }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        AppButton {
                            text: "保存默认值"
                            primary: true
                            onClicked: bridge.saveDefaults({
                                "img_model": imgModelCombo.currentText,
                                "img_size": imgSizeCombo.currentText,
                                "vid_model": vidModelCombo.currentText,
                                "vid_seconds": vidSecondsCombo.currentText,
                                "vid_aspect": vidAspectCombo.currentText
                            })
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                // ---------------- 找参考与上下文 ----------------
                AppCard {
                    id: contextCard
                    title: "找参考与上下文"
                    Text {
                        width: parent.width
                        text: "助手「找参考」时默认用哪些来源。关掉的来源根本不会去查"
                              + "（省额度也省时间）；草稿模式下还可以在本次上下文卡里临时改。"
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }

                    RowLayout {
                        width: parent.width
                        spacing: 12
                        Text { text: "模式"; color: theme.textSub; font.pixelSize: 12 }
                        AppCombo {
                            id: contextModeCombo
                            Layout.preferredWidth: 240
                            model: page.contextModeLabels
                        }
                        Text { text: "本地参考少于"; color: theme.textSub; font.pixelSize: 12 }
                        AppCombo {
                            id: contextThresholdCombo
                            Layout.preferredWidth: 80
                            model: page.refThresholds
                        }
                        Text { text: "条就联网"; color: theme.textSub; font.pixelSize: 12 }
                        Item { Layout.fillWidth: true }
                    }

                    RowLayout {
                        width: parent.width
                        spacing: 14
                        Text { text: "默认来源"; color: theme.textSub; font.pixelSize: 12 }
                        CheckBox {
                            id: historySourceBox
                            text: "历史参考"
                            font.pixelSize: 12
                        }
                        CheckBox {
                            id: knowledgeSourceBox
                            text: "知识库"
                            font.pixelSize: 12
                        }
                        CheckBox {
                            id: webSourceBox
                            text: "联网线索"
                            font.pixelSize: 12
                        }
                        CheckBox {
                            id: webImagesSourceBox
                            text: "联网配图"
                            font.pixelSize: 12
                            onToggled: {
                                // 首次打开先把免责声明摊开让他确认（方案 6.2）；
                                // 用户点取消就当没开过，点确定才记下「已读过」。
                                if (checked && !settingsBridge.webImagesAcknowledged)
                                    webImagesDialog.open();
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }

                    Text {
                        width: parent.width
                        text: "「联网配图」默认关闭。" + page.webImagesDisclaimer
                        color: theme.textDisabled
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }

                    RowLayout {
                        width: parent.width
                        spacing: 12
                        Text { text: "联网搜索"; color: theme.textSub; font.pixelSize: 12 }
                        AppCombo {
                            id: searchProviderCombo
                            Layout.preferredWidth: 150
                            model: page.searchProviderLabels
                        }
                        AppField {
                            id: searchKeyField
                            Layout.preferredWidth: 190
                            echoMode: TextInput.Password
                            placeholderText: "SEARCH_API_KEY"
                        }
                        AppField {
                            id: searchBaseField
                            Layout.preferredWidth: 230
                            placeholderText: "SEARCH_BASE_URL（custom 时必填）"
                        }
                        Item { Layout.fillWidth: true }
                    }

                    Text {
                        width: parent.width
                        text: "provider 换成 off 就完全不联网。没配 key 时助手不会假装搜过——"
                              + "草稿上会写明「没配联网搜索的 key，已跳过」。"
                        color: theme.textDisabled
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }

                    RowLayout {
                        width: parent.width
                        spacing: 12
                        AppButton {
                            text: "保存"
                            primary: true
                            onClicked: bridge.saveContext({
                                "mode": page.contextModeCodes[contextModeCombo.currentIndex],
                                "sources": {
                                    "history": historySourceBox.checked,
                                    "knowledge": knowledgeSourceBox.checked,
                                    "web": webSourceBox.checked,
                                    "web_images": webImagesSourceBox.checked
                                },
                                "min_local_refs": contextThresholdCombo.currentIndex
                            })
                        }
                        AppButton {
                            text: "保存联网设置"
                            onClicked: bridge.saveSearch({
                                "provider": page.searchProviderCodes[searchProviderCombo.currentIndex],
                                "api_key": searchKeyField.text,
                                "base_url": searchBaseField.text
                            })
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                // ---------------- 自更新 ----------------
                AppCard {
                    title: "自更新（提示词补丁）"

                    Text {
                        width: parent.width
                        text: "助手跑够 " + selfUpdateBridge.sampleNeeded + " 次带信号的运行后，"
                              + "可以让它总结规律、给出一份提示词补丁草案。"
                              + "补丁只在你点「接受」后才生效，旧版本保留、可回滚。"
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Text {
                            text: "当前版本："
                                  + (selfUpdateBridge.currentVersion > 0
                                     ? "v" + selfUpdateBridge.currentVersion
                                     : "出厂")
                            color: theme.textMain
                            font.pixelSize: 13
                            font.bold: true
                        }
                        Text {
                            Layout.fillWidth: true
                            text: selfUpdateBridge.currentText
                            color: theme.textSub
                            font.pixelSize: 12
                            wrapMode: Text.Wrap
                        }
                    }
                    Text {
                        width: parent.width
                        visible: selfUpdateBridge.currentReason !== ""
                        text: "依据：" + selfUpdateBridge.currentReason
                        color: theme.textDisabled
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }

                    Text {
                        width: parent.width
                        text: {
                            var have = selfUpdateBridge.sampleCount;
                            var need = selfUpdateBridge.sampleNeeded;
                            return have >= need
                                ? "带信号的运行 " + have + " 次，可以生成建议了"
                                : "带信号的运行 " + have + " / " + need
                                  + " 次，还差 " + (need - have) + " 次"
                        }
                        color: theme.textSub
                        font.pixelSize: 12
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        AppButton {
                            primary: true
                            text: selfUpdateBridge.busy ? "分析中……" : "分析并生成建议"
                            enabled: !selfUpdateBridge.busy
                                     && selfUpdateBridge.sampleCount >= selfUpdateBridge.sampleNeeded
                            onClicked: selfUpdateBridge.generateSuggestion()
                        }
                        AppButton {
                            text: "回滚到上一版"
                            visible: selfUpdateBridge.canRollback
                            onClicked: selfUpdateBridge.rollback()
                        }
                        Item { Layout.fillWidth: true }
                    }

                    // 待决定的建议
                    Rectangle {
                        id: pendingBox
                        objectName: "selfUpdatePending"
                        // 卡片体是普通 Column（不是 Layout），Layout.fillWidth 在这里没用，
                        // 必须显式给宽度——否则这块宽度算出来是 0，建议与接受/拒绝按钮全看不见。
                        width: parent.width
                        visible: selfUpdateBridge.hasPending
                        radius: theme.radiusControl
                        color: theme.accentSoft
                        border.width: 1
                        border.color: theme.accent
                        implicitHeight: pendingColumn.implicitHeight + 20

                        ColumnLayout {
                            id: pendingColumn
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 10
                            spacing: 8

                            Text {
                                Layout.fillWidth: true
                                text: "有一条建议等你决定："
                                color: theme.textMain
                                font.pixelSize: 13
                                font.bold: true
                            }
                            Text {
                                Layout.fillWidth: true
                                text: selfUpdateBridge.pendingText
                                color: theme.textMain
                                font.pixelSize: 12
                                wrapMode: Text.Wrap
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                AppButton {
                                    primary: true
                                    text: "接受（下次运行生效）"
                                    onClicked: selfUpdateBridge.acceptPending()
                                }
                                AppButton {
                                    text: "拒绝"
                                    onClicked: selfUpdateBridge.rejectPending()
                                }
                                Item { Layout.fillWidth: true }
                            }
                        }
                    }

                    // 版本历史
                    Column {
                        width: parent.width
                        spacing: 6
                        visible: page.versionList.length > 0

                        Text {
                            text: "历史"
                            color: theme.textSub
                            font.pixelSize: 12
                            font.bold: true
                        }
                        Repeater {
                            model: page.versionList
                            RowLayout {
                                width: parent.width
                                spacing: 8
                                Text {
                                    text: (modelData.version > 0 ? "v" + modelData.version : "—")
                                          + " · " + page.statusLabel(modelData.status)
                                          + " · " + modelData.when
                                    color: theme.textSub
                                    font.pixelSize: 12
                                    Layout.preferredWidth: 190
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.text
                                    color: theme.textDisabled
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }
                            }
                        }
                    }
                }

                // ---------------- 存储 ----------------
                AppCard {
                    title: "存储"
                    Text {
                        width: parent.width
                        text: "历史记录与本地缓存（SQLite + media/thumbs）都放在这里。"
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Text {
                            Layout.fillWidth: true
                            text: values.data_dir || ""
                            color: theme.textMain
                            font.pixelSize: 12
                            elide: Text.ElideMiddle
                        }
                        AppButton {
                            text: "打开目录"
                            onClicked: bridge.openDataDir()
                        }
                    }
                }
            }
        }
    }

    Connections {
        target: bridge
        function onSaved(section) {
            probeResult.ok = true;
            probeResult.text = "已保存并生效";
            probeResult.visible = true;
            page.reload();
            backend.refreshCredentials();
        }
        function onProbeFinished(ok, message) {
            probeResult.ok = ok;
            probeResult.text = message;
            probeResult.visible = true;
        }
    }

    Connections {
        target: selfUpdateBridge
        function onStateChanged() {
            page.reloadVersions();
        }
        function onNoticeRaised(level, text) {
            probeResult.ok = level !== "error";
            probeResult.text = text;
            probeResult.visible = true;
        }
    }

    Rectangle {
        id: probeResult
        property bool ok: true
        property string text: ""
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 20
        height: Math.max(44, label.implicitHeight + 20)
        radius: theme.radiusControl
        color: ok ? theme.successBg : theme.dangerBg
        border.width: 1
        border.color: ok ? theme.success : theme.danger
        visible: false
        z: 50

        Text {
            id: label
            anchors.fill: parent
            anchors.margins: 10
            text: probeResult.text
            color: probeResult.ok ? theme.success : theme.dangerText
            font.pixelSize: 12
            wrapMode: Text.Wrap
        }
    }

    Component.onCompleted: {
        reload();
        reloadVersions();
    }

    /* 联网配图的免责声明：第一次打开那个开关时弹一次（方案 6.2）。
       文案与开关旁常驻的那段是同一份（Python 侧 WEB_IMAGES_DISCLAIMER），避免两处说法不一致。 */
    Dialog {
        id: webImagesDialog
        title: "联网配图：先看一眼免责声明"
        modal: true
        anchors.centerIn: parent
        width: 420
        standardButtons: Dialog.Ok | Dialog.Cancel
        Text {
            width: parent.width
            text: page.webImagesDisclaimer
            color: theme.textMain
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }
        onAccepted: settingsBridge.acknowledgeWebImages()
        onRejected: webImagesSourceBox.checked = false
    }
}
