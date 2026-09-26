import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "components"
import "pages"

ApplicationWindow {
    id: window

    width: 1200
    height: 820
    minimumWidth: 980
    minimumHeight: 660
    visible: true
    title: "Stillroom"
    color: theme.bg
    font.family: theme.fontFamily
    font.pixelSize: 13

    /* 只读探针：界面上几个「事实」，给测试与截图工具读（不参与任何业务逻辑）。
       走这里是刻意的——从 Python 侧 findChild 拿界面对象会被 shiboken 的包装缓存坑到
       （上一个引擎释放后地址被复用，返回的是已失效的旧包装），读属性值则没这个问题。 */
    readonly property int pageCount: stack.count
    readonly property int agentStepCount: agentPage.stepCount
    readonly property int agentContextItems: agentBridge.contextItems.length
    readonly property bool agentHasContext: agentBridge.hasContext
    readonly property bool agentCanEditContext: agentBridge.canEditContext
    readonly property int agentProfileCount: agentBridge.profiles.length
    readonly property real selfUpdatePendingWidth: settingsPage.pendingBoxWidth
    readonly property bool settingsContextCardReady: settingsPage.contextCardReady
    readonly property int settingsContextMode: settingsPage.contextModeIndex
    readonly property int settingsContextThreshold: settingsPage.contextThresholdIndex
    readonly property int settingsSearchProvider: settingsPage.searchProviderIndex
    readonly property bool settingsSearchKeySet: settingsPage.searchKeyConfigured
    readonly property bool webImagesAcknowledged: settingsPage.webImagesAcknowledged
    readonly property bool webImagesDialogOpen: settingsPage.webImagesDialogOpen

    /* 给界面测试用：确认「首次开启联网配图会弹免责声明」这条真的通。 */
    function askWebImagesConsent() {
        settingsPage.askWebImagesConsent();
    }
    readonly property int knowledgeDocCount: knowledgePage.docCount
    readonly property int knowledgeHitCount: knowledgePage.hitCount
    /* 视频页：上传确认框是否弹着、参考图有几条（给界面测试读） */
    readonly property bool videoUploadConfirmOpen: videoPage.uploadConfirmOpen
    readonly property int videoRefCount: videoPage.refCount
    /* 历史页灯箱里「另存为」是否可用（给界面测试读） */
    readonly property bool lightboxSaveAsAvailable: historyPage.lightboxSaveAsAvailable
    /* 历史灯箱的详情弹层（给界面测试读） */
    readonly property bool lightboxDetailOpen: historyPage.lightboxDetailOpen
    readonly property int lightboxDetailLength: historyPage.lightboxDetailLength
    /* 助手页结果卡：能否「另存为」（给界面测试读） */
    readonly property bool agentSaveAsAvailable: agentBridge.hasResult
    /* 图片页：提示词字数、参考图条数（给界面测试读） */
    readonly property int imagePromptCount: imagePage.promptCount
    readonly property int imageRefCount: imagePage.refCount

    /* 切到某一页：导航栏用，测试也用（同一入口，测试才不会绕过真实布局） */
    function openPage(index) {
        stack.currentIndex = index;
    }

    /* 给界面测试用：按真实入口打开历史灯箱（与点缩略图走的是同一条路）。 */
    function openHistoryRecord(recordId) {
        historyPage.openRecord(recordId);
    }

    /* 给界面测试用：走图片页「URL 输入框 + 添加」这条真实路径。 */
    function addImageReferenceUrl(text) {
        imagePage.addReferenceUrl(text);
    }

    /* 给界面测试用：打开灯箱详情（与点「详情」按钮同一条代码）。 */
    function openLightboxDetail() {
        historyPage.openLightboxDetail();
    }

    /* 全局 palette：让 QtQuick Controls 自带的弹出列表 / 滚动条也跟随主题
       （否则深色主题下下拉框依然是浅色） */
    palette.window: theme.card
    palette.windowText: theme.textMain
    palette.base: theme.field
    palette.alternateBase: theme.card
    palette.text: theme.textMain
    palette.button: theme.fill
    palette.buttonText: theme.textMain
    palette.highlight: theme.accent
    palette.highlightedText: theme.onAccent
    palette.mid: theme.cardBorder
    palette.placeholderText: theme.textDisabled

    /* ---------------- 顶栏 ---------------- */
    header: Rectangle {
        height: 54
        color: theme.card

        Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: 1
            color: theme.cardBorder
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 18
            anchors.rightMargin: 18
            spacing: 12

            Text {
                text: "Stillroom"
                color: theme.textMain
                font.pixelSize: 17
                font.bold: true
            }
            Text {
                text: "图片 · 视频生成"
                color: theme.textSub
                font.pixelSize: 12
            }

            Item { Layout.fillWidth: true }

            Rectangle {
                height: 24
                width: siteLabel.implicitWidth + 20
                radius: theme.radiusPill
                color: backend.configured ? theme.successBg : theme.dangerBg
                Text {
                    id: siteLabel
                    anchors.centerIn: parent
                    text: backend.configured ? backend.siteLabel : "未配置密钥"
                    color: backend.configured ? theme.success : theme.dangerText
                    font.pixelSize: 12
                }
            }

            AppButton {
                text: theme.dark ? "浅色" : "深色"
                onClicked: theme.toggle()
            }
        }
    }

    /* ---------------- 主体 ---------------- */
    RowLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.preferredWidth: 208
            Layout.fillHeight: true
            color: theme.card

            Rectangle {
                anchors.right: parent.right
                width: 1
                height: parent.height
                color: theme.cardBorder
            }

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 12
                spacing: 6

                Text {
                    text: "工作台"
                    color: theme.textSub
                    font.pixelSize: 12
                    Layout.leftMargin: 8
                    Layout.bottomMargin: 4
                }

                NavButton {
                    Layout.fillWidth: true
                    text: "助手"
                    current: stack.currentIndex === 0
                    onClicked: stack.currentIndex = 0
                }
                NavButton {
                    Layout.fillWidth: true
                    text: "图片生成"
                    current: stack.currentIndex === 1
                    onClicked: stack.currentIndex = 1
                }
                NavButton {
                    Layout.fillWidth: true
                    text: "视频生成"
                    current: stack.currentIndex === 2
                    onClicked: stack.currentIndex = 2
                }
                NavButton {
                    Layout.fillWidth: true
                    text: "历史记录"
                    current: stack.currentIndex === 3
                    onClicked: stack.currentIndex = 3
                }
                NavButton {
                    Layout.fillWidth: true
                    text: "知识库"
                    current: stack.currentIndex === 4
                    onClicked: stack.currentIndex = 4
                }
                NavButton {
                    Layout.fillWidth: true
                    text: "设置"
                    current: stack.currentIndex === 5
                    onClicked: stack.currentIndex = 5
                }

                Item { Layout.fillHeight: true }

                Text {
                    Layout.fillWidth: true
                    Layout.leftMargin: 8
                    text: "Stillroom · Qt 6"
                    color: theme.textDisabled
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
            }
        }

        StackLayout {
            id: stack
            objectName: "pageStack"
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: 0

            AgentPage { id: agentPage }
            ImagePage { id: imagePage }
            VideoPage { id: videoPage }
            HistoryPage { id: historyPage }
            KnowledgePage { id: knowledgePage }
            SettingsPage { id: settingsPage }
        }
    }

    /* ---------------- 状态栏 ---------------- */
    footer: Rectangle {
        height: 36
        color: theme.card

        Rectangle {
            anchors.top: parent.top
            width: parent.width
            height: 1
            color: theme.cardBorder
        }

        ProgressBar {
            id: progressBar
            anchors.left: parent.left
            anchors.leftMargin: 18
            anchors.verticalCenter: parent.verticalCenter
            width: 180
            height: 6
            visible: backend.busy || agentBridge.running
            indeterminate: backend.busy ? backend.progress < 0 : agentBridge.progress < 0
            from: 0
            to: 100
            value: backend.busy ? (backend.progress >= 0 ? backend.progress : 0)
                                : (agentBridge.progress >= 0 ? agentBridge.progress : 0)

            background: Rectangle {
                radius: 3
                color: theme.fill
            }
            contentItem: Item {
                Rectangle {
                    visible: !progressBar.indeterminate
                    width: progressBar.visualPosition * parent.width
                    height: parent.height
                    radius: 3
                    color: theme.accent
                }
                Rectangle {
                    id: indeterminateBar
                    visible: progressBar.indeterminate
                    width: parent.width * 0.35
                    height: parent.height
                    radius: 3
                    color: theme.accent
                    property real travel: Math.max(0, parent.width - width)
                    SequentialAnimation on x {
                        running: progressBar.indeterminate
                        loops: Animation.Infinite
                        NumberAnimation { from: 0; to: indeterminateBar.travel; duration: 900; easing.type: Easing.InOutQuad }
                        NumberAnimation { from: indeterminateBar.travel; to: 0; duration: 900; easing.type: Easing.InOutQuad }
                    }
                }
            }
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: (backend.busy || agentBridge.running) ? 210 : 18
            anchors.rightMargin: 18
            spacing: 12

            Text {
                Layout.fillWidth: true
                text: {
                    if (agentBridge.running)
                        return "助手正在跑：" + agentBridge.statusText
                               + (agentBridge.stepCount > 0 ? "（第 " + agentBridge.stepCount + " 步）" : "");
                    if (backend.busy)
                        return backend.message !== "" ? backend.message : backend.statusText;
                    if (backend.status === "succeeded")
                        return "已完成";
                    if (backend.status === "failed")
                        return "失败：" + backend.errorText;
                    if (backend.status === "canceled")
                        return "已取消";
                    return "就绪";
                }
                color: backend.status === "failed" ? theme.dangerText : theme.textSub
                font.pixelSize: 12
                elide: Text.ElideRight
            }

            Text {
                text: backend.dataDir
                color: theme.textDisabled
                font.pixelSize: 11
                elide: Text.ElideMiddle
                Layout.maximumWidth: 320
            }
        }
    }

    Toast {
        id: toast
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.topMargin: 70
    }

    Connections {
        target: backend
        function onNoticeRaised(level, text) {
            toast.show(level, text);
        }
        function onParamsLoaded(kind, params) {
            stack.currentIndex = kind === "video" ? 2 : 1;
        }
    }

    Connections {
        target: agentBridge
        function onNoticeRaised(level, text) {
            toast.show(level, text);
        }
    }

    Connections {
        target: knowledgeBridge
        function onNoticeRaised(level, text) {
            toast.show(level, text);
        }
    }
}
