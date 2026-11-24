# Cấu hình

Tài liệu này mô tả các khoá trong thư mục `config/` và tác dụng của chúng. Giá trị mặc định là giá trị trong file của bản phát hành này.

Đây không phải danh sách đầy đủ mọi khoá. `config/settings.json` có 177 khoá và `config/global_settings.json` có 110 khoá. Các khoá không có trong bảng vẫn có trong file và được bot đọc theo cách đặt tên của chúng.

## Cách đọc bảng

- **Khoá** là đường dẫn đầy đủ. `commands.hunt.enabled` nghĩa là `"commands"`, rồi `"hunt"`, rồi `"enabled"` trong file JSON.
- **Kiểu** là `bool` (`true` hoặc `false`), `int` (số nguyên), `float` (số thực), `str` (chuỗi), `null`, hoặc `list` (danh sách). Khoảng chờ ghi dạng `[tối thiểu, tối đa]`, và bot chọn ngẫu nhiên trong khoảng đó.
- **Mặc định (bản phát hành)** là giá trị có sẵn trong file.
- Sau khi sửa file, khởi động lại bot để áp dụng.

## Thứ tự ưu tiên

- `config/settings.json` là cài đặt mặc định cho mọi tài khoản.
- `config/<ID tài khoản>.settings.json` thay thế hoàn toàn `settings.json` cho tài khoản có ID đó. Bot không gộp hai file. Vì vậy file riêng phải chứa đủ các khoá cần dùng. Cách dễ nhất là sao chép `settings.json`, đổi tên, rồi sửa.

## config/settings.json

Cài đặt cho mỗi tài khoản. Các khoá có kiểu `list` chứa ID người dùng hoặc ID kênh không được ghi giá trị ở đây.

### Chung

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `setprefix` | str | `"owo"` | Tiền tố đặt trước tên lệnh khi gửi tới OwO bot, ví dụ `owo hunt`. |
| `useSlashCommands` | bool | `false` | Dùng lệnh slash của Discord khi lệnh đó hỗ trợ. |
| `claimMail` | bool | `true` | Tự nhận thư (mail). |
| `autoDaily` | bool | `true` | Tự nhận daily. |
| `cashCheck` | bool | `true` | Kiểm tra số cash trước khi mua hoặc bán. Dùng bởi lệnh `shop` và `sell`. |

### Lệnh

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `commands.hunt.enabled` | bool | `true` | Bật lệnh `hunt`. |
| `commands.hunt.cooldown` | list | `[17, 43]` | Khoảng chờ giữa hai lần gửi `hunt`, tính bằng giây. |
| `commands.hunt.useShortForm` | bool | `false` | Dùng tên viết tắt `h` thay cho `hunt`. |
| `commands.battle.enabled` | bool | `true` | Bật lệnh `battle`. |
| `commands.battle.cooldown` | list | `[17, 43]` | Khoảng chờ giữa hai lần gửi `battle`. |
| `commands.battle.useShortForm` | bool | `false` | Dùng tên viết tắt `b` thay cho `battle`. |
| `commands.battle.showStreakInConsole` | bool | `false` | In chuỗi thắng hoặc thua trên console. |
| `commands.battle.notifyStreakLoss` | bool | `false` | Thông báo khi thua liên tiếp. |
| `commands.pray.enabled` | bool | `true` | Bật lệnh `pray`. |
| `commands.pray.cooldown` | list | `[367, 555]` | Khoảng chờ giữa hai lần gửi `pray`. |
| `commands.pray.pingUser` | bool | `false` | Nhắc (ping) người dùng trong lệnh `pray`. |
| `commands.pray.customChannel.enabled` | bool | `false` | Gửi lệnh `pray` ở một kênh khác kênh chính, kênh là `commands.pray.customChannel.channelId`. |
| `commands.curse.enabled` | bool | `false` | Bật lệnh `curse`. |
| `commands.curse.cooldown` | list | `[310, 400]` | Khoảng chờ giữa hai lần gửi `curse`. |
| `commands.owo.enabled` | bool | `true` | Bật lệnh `owo`. |
| `commands.owo.cooldown` | list | `[13, 46]` | Khoảng chờ giữa hai lần gửi `owo`. |
| `commands.lottery.enabled` | bool | `false` | Bật lệnh xổ số `lottery`. |
| `commands.lottery.amount` | int | `1` | Số lượng mỗi lần chơi xổ số. |
| `commands.lvlGrind.enabled` | bool | `true` | Bật tăng cấp: gửi câu hoặc chuỗi ký tự để nhận XP. |
| `commands.lvlGrind.cooldown` | list | `[13, 66]` | Khoảng chờ giữa hai lần tăng cấp. |
| `commands.lvlGrind.useQuoteInstead` | bool | `true` | Dùng câu trích dẫn và `config/level_lines.txt` thay cho chuỗi ký tự ngẫu nhiên. |
| `commands.lvlGrind.minLengthForRandomString` | int | `5` | Độ dài tối thiểu của chuỗi ngẫu nhiên, khi không dùng câu. |
| `commands.lvlGrind.maxLengthForRandomString` | int | `20` | Độ dài tối đa của chuỗi ngẫu nhiên, khi không dùng câu. |
| `commands.lvlGrind.quoteSource` | str | `"api"` | Nguồn câu nói cho lệnh tăng cấp: `api` lấy câu nói tiếng Việt từ `blv-anh-ngok-said.onrender.com` (10 câu, lấy lại mỗi 6 giờ; lỗi mạng thì dùng `config/level_lines.txt`), `file` chỉ đọc `config/level_lines.txt`. Bot chỉ gọi dịch vụ khi `commands.lvlGrind.enabled` và `useQuoteInstead` đều bật. Tắt hẳn bằng `allow_quotes` trong `config/danger.toml`. |
| `commands.cookie.enabled` | bool | `false` | Bật lệnh `cookie`. |
| `commands.cookie.pingUser` | bool | `false` | Nhắc người dùng trong lệnh `cookie`. |
| `commands.shop.enabled` | bool | `false` | Bật mua vật phẩm trong cửa hàng `shop`. |
| `commands.shop.cooldown` | list | `[10, 16]` | Khoảng chờ giữa hai lần gửi `shop`. |
| `commands.shop.itemsToBuy.commonRing` | bool | `false` | Mua vòng `commonRing`. Sáu vòng còn lại (`uncommonRing`, `rareRing`, `epicRing`, `mythicalRing`, `legendaryRing`, `fabledRing`) có cùng cách đặt khoá, mặc định cũng tắt. |
| `commands.autoHuntBot.enabled` | bool | `true` | Bật Hunt Bot tự động. |
| `commands.autoHuntBot.cashToSpend` | int | `10000` | Số cash tối đa dành cho Hunt Bot. |
| `commands.autoHuntBot.upgrader.enabled` | bool | `true` | Tự nâng cấp Hunt Bot. |
| `commands.autoHuntBot.upgrader.sleeptime` | list | `[10, 15]` | Khoảng chờ của bộ nâng cấp Hunt Bot, tính bằng giây. |
| `commands.pup.enabled` | bool | `false` | Bật lệnh `pup`. |
| `commands.piku.enabled` | bool | `false` | Bật lệnh `piku`. |
| `commands.run.enabled` | bool | `false` | Bật lệnh `run`. |
| `commands.army.enabled` | bool | `false` | Bật lệnh `army`. |

### Thú săn, trò chơi may rủi, các tính năng khác

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `animal.sell.enabled` | bool | `false` | Bán thú săn theo độ hiếm. Các loại được chọn nằm trong `animal.sell.rarity`. |
| `animal.sac.enabled` | bool | `true` | Hiến tế thú săn theo độ hiếm. Các loại được chọn nằm trong `animal.sac.rarity`. |
| `animal.cooldown` | list | `[33, 77]` | Khoảng chờ cho lệnh liên quan đến thú săn, tính bằng giây. |
| `gamble.allottedAmount` | int | `10000` | Số cash tối đa dành cho các trò may rủi. |
| `gamble.goalSystem.enabled` | bool | `false` | Bật hệ thống mục tiêu cho các trò may rủi. |
| `gamble.goalSystem.amount` | int | `30000` | Mức cash mục tiêu của hệ thống trên. |
| `gamble.coinflip.enabled` | bool | `false` | Bật trò `coinflip`. |
| `gamble.coinflip.startValue` | int | `200` | Mức cược ban đầu của `coinflip`. |
| `gamble.coinflip.multiplierOnLose` | int | `2` | Hệ số nhân mức cược sau khi thua `coinflip`. |
| `gamble.coinflip.cooldown` | list | `[16, 18]` | Khoảng chờ của `coinflip`. |
| `gamble.coinflip.options.heads` | bool | `true` | Cho phép lựa chọn `heads`. |
| `gamble.coinflip.options.tails` | bool | `false` | Cho phép lựa chọn `tails`. |
| `gamble.slots.enabled` | bool | `false` | Bật trò `slots`. |
| `gamble.slots.startValue` | int | `200` | Mức cược ban đầu của `slots`. |
| `gamble.slots.multiplierOnLose` | int | `2` | Hệ số nhân mức cược sau khi thua `slots`. |
| `gamble.slots.cooldown` | list | `[16, 18]` | Khoảng chờ của `slots`. |
| `gamble.blackjack.enabled` | bool | `false` | Bật trò `blackjack`. |
| `gamble.blackjack.startValue` | int | `200` | Mức cược ban đầu của `blackjack`. |
| `gamble.blackjack.multiplierOnLose` | int | `2` | Hệ số nhân mức cược sau khi thua `blackjack`. |
| `gamble.blackjack.cooldown` | list | `[16, 18]` | Khoảng chờ của `blackjack`. |
| `bossBattle.enabled` | bool | `true` | Bật tham gia boss battle. |
| `bossBattle.joinChancePercent` | int | `100` | Xác suất tham gia boss, tính bằng phần trăm. |
| `bossBattle.joinAllGuilds` | bool | `false` | Tham gia boss ở mọi máy chủ. |
| `bossBattle.guildJoinRules.ignoreGuildIds` | list | `[]` | Danh sách ID máy chủ bỏ qua. |
| `bossBattle.guildJoinRules.onlyJoinGuildIds` | list | `[]` | Chỉ tham gia boss ở các máy chủ trong danh sách. |
| `giveawayJoiner.enabled` | bool | `false` | Tự tham gia giveaway trong các kênh đã chọn. |
| `giveawayJoiner.channelsToJoin` | list | `[]` | Danh sách ID kênh có giveaway. |
| `giveawayJoiner.cooldown` | list | `[40, 100]` | Khoảng chờ giữa hai lần tham gia, tính bằng giây. |
| `giveawayJoiner.messageRangeToCheck` | int | `6` | Số tin nhắn gần nhất cần kiểm tra. |
| `sleep.enabled` | bool | `true` | Bật tính năng ngủ ngẫu nhiên. |
| `sleep.frequencyPercentage` | int | `5` | Tỉ lệ phần trăm dùng cho tính năng ngủ. |
| `sleep.checkTime` | list | `[10, 20]` | Khoảng `[tối thiểu, tối đa]` dùng cho tính năng ngủ. |
| `sleep.sleepTime` | list | `[9, 17]` | Khoảng `[tối thiểu, tối đa]` cho thời gian ngủ. |
| `misspell.enabled` | bool | `false` | Bật mô phỏng gõ sai chính tả. |
| `misspell.frequencyPercentage` | int | `1` | Tỉ lệ gõ sai, tính bằng phần trăm. |
| `autoQuest.enabled` | bool | `false` | Tự làm nhiệm vụ (quest). Tính năng thử nghiệm. Cần `allow_auto_quest` là `true` trong `config/danger.toml`. |
| `autoQuest.checkCooldown` | list | `[10, 30]` | Khoảng chờ giữa hai lần kiểm tra nhiệm vụ, tính bằng giây. |
| `autoQuest.helpOthers` | bool | `false` | Các tài khoản trong `auto_quest_accounts` giúp nhau làm nhiệm vụ. Không dùng được cùng reactionbot. |
| `autoQuest.enableCommandsToCompleteQuest` | bool | `false` | Cho phép bot gửi các lệnh cần để hoàn thành nhiệm vụ. Một số nhiệm vụ cần tài khoản đối tác. |
| `autoUse.gems.enabled` | bool | `true` | Tự dùng gem. |
| `autoUse.gems.minHuntsBeforeReuse` | int | `20` | Số lần săn tối thiểu trước khi dùng lại một loại gem. |
| `autoUse.gems.maxSpecialPerHour` | int | `2` | Số gem đặc biệt tối đa dùng mỗi giờ. |
| `autoUse.gems.disableHuntIfNoGems` | bool | `false` | Tắt săn khi hết gem. |
| `autoUse.autoLootbox` | bool | `true` | Tự mở lootbox. |
| `autoUse.autoCrate` | bool | `true` | Tự mở crate. |
| `autoUse.autoFabledLootbox` | bool | `true` | Tự mở fabled lootbox bằng lệnh trong `autoUse.fabledLootboxCommand`. |
| `autoUse.fabledLootboxCommand` | str | `"lootbox f"` | Lệnh dùng để mở fabled lootbox. |
| `autoUse.inventoryCheckCooldown` | int | `600` | Chu kỳ kiểm tra kho đồ, tính bằng giây. |
| `customCommands.enabled` | bool | `false` | Bật lệnh tuỳ chỉnh. |
| `customCommands.commands` | list | (3 mục mẫu) | Danh sách lệnh tuỳ chỉnh. |

### Khoảng chờ mặc định

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `defaultCooldowns.longCooldown` | list | `[400, 600]` | Khoảng chờ dài, dùng khi lệnh không có khoảng riêng. |
| `defaultCooldowns.moderateCooldown` | list | `[70, 200]` | Khoảng chờ vừa. |
| `defaultCooldowns.shortCooldown` | list | `[10, 60]` | Khoảng chờ ngắn. |
| `defaultCooldowns.briefCooldown` | list | `[2.7, 9.3]` | Khoảng chờ rất ngắn. |
| `defaultCooldowns.commandHandler.betweenCommands` | list | `[3.6, 6.7]` | Khoảng chờ giữa hai lệnh trong bộ xử lý lệnh. |
| `defaultCooldowns.reactionBot.cooldown` | list | `[0.5, 1]` | Khoảng chờ của bộ phản hồi tự động (`reactionBot`). |

## config/global_settings.json

Cài đặt chung cho mọi tài khoản. Các khoá có ghi chú **Khoá** là khoá bị khoá trên dashboard, chỉ sửa tay trong file.

### Chung

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `typingIndicator` | bool | `false` | Hiện trạng thái "đang gõ" trước khi gửi lệnh. |
| `silentMessages` | bool | `false` | Gửi tin nhắn với tham số `silent` của thư viện. Hiệu ứng cụ thể chưa được mô tả trong tài liệu này. |
| `offlineStatus` | bool | `false` | Đặt trạng thái ngoại tuyến khi bot chạy. |
| `ocrApi` | str | `""` | Khoá API của ocr.space, dùng cho nhiệm vụ (quest) khi Tesseract không đọc được ảnh. Để trống thì bot không gọi ocr.space và không dùng khoá thử nghiệm; quest chỉ dùng Tesseract, và bot in một dòng thông báo mỗi lần chạy. Đây là bí mật. |

### website

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `website.enabled` | bool | `true` | Bật dashboard web. |
| `website.host` | str | `"0.0.0.0"` | Địa chỉ IPv4 mà dashboard nghe. `0.0.0.0` cho máy khác trong mạng mở được (khi có mật khẩu từ 8 ký tự trở lên), `127.0.0.1` giữ dashboard trên máy này. Thiếu hoặc để trống thì dùng `0.0.0.0`; không có mật khẩu đủ dài thì vẫn chỉ mở trên máy này. |
| `website.port` | int | `1200` | Cổng của dashboard. |
| `website.refreshInterval` | int | `15` | Chu kỳ làm mới dashboard, tính bằng giây. |
| `website.password` | str | `""` | Mật khẩu dashboard. Để trống hoặc ngắn hơn 8 ký tự thì dashboard chỉ mở trên máy này (`127.0.0.1`). Mở cho máy khác trong mạng cần mật khẩu từ 8 ký tự trở lên. Đây là bí mật. |
| `website.separateProcess` | bool | `true` | Chạy dashboard trong tiến trình riêng. Khi tắt, dashboard chạy trong cùng tiến trình với bot. |
| `website.enableHost` | bool | `false` | Không còn dùng để chọn địa chỉ nghe: chỉ `website.host` quyết định. Khoá này còn được đọc để file cũ vẫn nạp được. |

### database và account

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `database.logStatistics` | bool | `true` | Ghi thống kê vào `utils/data/db.sqlite`. |
| `account.pauseTimeoutSeconds` | int | `60` | Giới hạn thời gian, tính bằng giây, cho một lần tạm dừng gửi lệnh. Xem `core/command_handler.py`. |
| `account.stallThresholdSeconds` | int | `600` | Số giây không có hoạt động trước khi coi tài khoản bị treo. |
| `account.waveSize` | int | `15` | Số tài khoản khởi động trong một đợt. |
| `account.waveDelay` | list | `[3.0, 6.0]` | Khoảng chờ ngẫu nhiên giữa hai đợt, tính bằng giây. |
| `account.loginMaxRetries` | int | `3` | Số lần thử đăng nhập tối đa của một tài khoản. |
| `account.globalRatePerSecond` | float | `3.0` | Giới hạn số lần gửi chung của mọi tài khoản trong một giây. |
| `account.firstSendStaggerStep` | float | `0.25` | Khoảng lệch, tính bằng giây, giữa các tài khoản khi gửi lệnh đầu tiên. |
| `account.firstSendStaggerCap` | float | `600.0` | Giới hạn tối đa, tính bằng giây, của độ lệch trên. |
| `account.startupDelay` | list | `[1, 2]` | Khoảng chờ ngẫu nhiên, tính bằng giây, trước khi khởi động một tài khoản. |
| `account.commandsStartDelay` | list | `[0.1, 0.2]` | Khoảng chờ, tính bằng giây, trước khi tài khoản bắt đầu gửi lệnh. |
| `account.commandsHandlerStartDelay` | list | `[5.6, 7.8]` | Khoảng chờ, tính bằng giây, trước khi bộ xử lý lệnh bắt đầu. |

### replyFallback

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `replyFallback.enabled` | bool | `true` | Đọc lại lịch sử kênh để tìm phản hồi của OwO mà kết nối không chuyển tới. |
| `replyFallback.firstCheckAfter` | int | `15` | Số giây sau lệnh thì bắt đầu đọc lịch sử. |
| `replyFallback.checkEvery` | int | `15` | Chu kỳ, tính bằng giây, giữa hai lần đọc lịch sử. |
| `replyFallback.giveUpAfter` | int | `60` | Sau bao nhiêu giây thì bỏ qua lệnh không có phản hồi. |
| `replyFallback.historyLimit` | int | `15` | Số tin nhắn lịch sử đọc mỗi lần. |
| `replyFallback.maxConcurrentFetches` | int | `4` | Số lần đọc lịch sử chạy song song tối đa. |
| `replyFallback.holdWhileGatewayDown` | bool | `false` | Giữ lệnh chờ khi kết nối gateway bị ngắt. |
| `replyFallback.dmCaptchaCheckEvery` | int | `60` | Chu kỳ, tính bằng giây, kiểm tra tin nhắn riêng. |

### textCommands

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `textCommands.prefix` | str | `"."` | Tiền tố của lệnh điều khiển bằng tin nhắn. |
| `textCommands.commandToStopUser` | str | `"stop"` | Lệnh dừng tài khoản. Gõ tiền tố rồi tên lệnh, ví dụ `.stop`. |
| `textCommands.commandToStartUser` | str | `"start"` | Lệnh chạy lại tài khoản. |
| `textCommands.commandToRestartAfterCaptcha` | str | `"restart_captcha"` | Lệnh chạy lại tài khoản sau khi xử lý captcha. |
| `textCommands.commandToSleep` | str | `"sleep"` | Lệnh cho tài khoản ngủ. |
| `textCommands.CommandToSwitchChannel` | str | `"switch_channel"` | Lệnh đổi kênh. Chú ý chữ `C` viết hoa trong tên khoá. |
| `textCommands.defaultSleepDuration` | int | `600` | Thời gian ngủ mặc định, tính bằng giây, khi lệnh ngủ không ghi thời gian. |
| `textCommands.allowedUsers` | list | `[]` | Danh sách ID người dùng khác được phép gửi lệnh điều khiển. Tài khoản của bạn luôn được phép. |

### webhook

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `webhook.enabled` | bool | `false` | Bật thông báo qua webhook Discord. |
| `webhook.logCommandSend` | bool | `false` | Gửi thông báo qua webhook mỗi khi gửi lệnh. Dùng mẫu `on_command_send`. |
| `webhook.webhookUrl` | str | `""` | URL webhook Discord chính. Đây là bí mật. |
| `webhook.webhookCaptchaUrl` | null | `null` | URL webhook riêng cho thông báo captcha, nếu có. Đây là bí mật. |
| `webhook.webhookUserIdToPingOnCaptcha` | str | `""` | ID người dùng được nhắc khi có thông báo captcha. Phải là ID hợp lệ. |
| `webhook.animalLog.enabled` | bool | `true` | Gửi thông báo khi bắt được thú từ lệnh hunt. |
| `webhook.animalLog.rank.fabled` | bool | `true` | Gửi thông báo cho thú hạng `fabled`. |
| `webhook.animalLog.rank.hidden` | bool | `true` | Gửi thông báo cho thú hạng `hidden`. |
| `webhook.animalLog.rank.distorted` | bool | `true` | Gửi thông báo cho thú hạng `distorted`. Các hạng còn lại mặc định tắt. |
| `webhook.others.logLootbox` | bool | `true` | Gửi thông báo khi tìm được lootbox. |
| `webhook.others.logCrate` | bool | `true` | Gửi thông báo khi tìm được crate. |
| `webhook.others.logChannelSwitch` | bool | `true` | Gửi thông báo khi đổi kênh. |

### console

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `console.runConsoleCommandOnCaptcha` | bool | `false` | Khoá, chỉ sửa tay. Chạy một lệnh trên máy khi có captcha. |
| `console.commandToRunOnCaptcha` | str | `""` | Khoá, chỉ sửa tay. Lệnh chạy trên máy khi có captcha. |
| `console.runConsoleCommandOnBan` | bool | `false` | Khoá, chỉ sửa tay. Chạy một lệnh trên máy khi tài khoản bị ban. |
| `console.commandToRunOnBan` | str | `""` | Khoá, chỉ sửa tay. Lệnh chạy trên máy khi tài khoản bị ban. |

### captcha

Phần này chỉ mô tả các khoá. Luồng xử lý captcha được nêu ở [Khi gặp captcha](../README.md#khi-gặp-captcha) trong README.

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `captcha.openCaptchaWebsite` | bool | `false` | Mở trang captcha bằng trình duyệt trên máy. |
| `captcha.timeoutSeconds` | int | `600` | Thời gian tối đa, tính bằng giây, cho một lần xử lý qua nhà cung cấp bên thứ ba. Captcha ảnh và lần chỉ thông báo không có hạn này. |
| `captcha.flagOnTimeout` | bool | `true` | Khi hết hạn, đánh dấu tài khoản và gửi thông báo `on_captcha_timeout_flag`. Tắt thì chỉ giữ hạn. |
| `captcha.stopCodeIfFailedToSolve` | bool | `true` | Khi một lần xử lý thất bại, dừng theo `captcha.stopScope`. |
| `captcha.stopScope` | str | `"account"` | `account` là chỉ dừng tài khoản đó. `process` là dừng cả tiến trình. Chỉ nhận hai giá trị này. |
| `captcha.notifications.enabled` | bool | `true` | Bật thông báo captcha. |
| `captcha.notifications.captchaContent` | str | `"Captcha Detected: {username} in {channelname}, captcha type: {captchatype}"` | Nội dung thông báo. Các ô `{username}`, `{channelname}` và `{captchatype}` được thay bằng giá trị thực. |
| `captcha.notifications.bannedContent` | str | `"{username} Banned from OwO!"` | Nội dung thông báo khi bị ban. |
| `captcha.notifications.reccur.enabled` | bool | `true` | Lặp lại thông báo captcha. |
| `captcha.notifications.reccur.timesToReccur` | int | `3` | Số lần lặp lại thông báo. |
| `captcha.playAudio.enabled` | bool | `true` | Phát âm thanh khi có captcha. |
| `captcha.playAudio.path` | str | `"Beep.mp3"` | Tên file trong thư mục `audio/`, hoặc đường dẫn đầy đủ tới file. |
| `captcha.toastOrPopup.enabled` | bool | `true` | Hiện cửa sổ thông báo. Trên Termux, hiện toast. |
| `captcha.toastOrPopup.captchaContent` | str | `"Captcha Detected! {username}"` | Nội dung cửa sổ thông báo. |
| `captcha.toastOrPopup.bannedContent` | str | `"{username} Banned from OwO!"` | Nội dung cửa sổ khi bị ban. |
| `captcha.toastOrPopup.termuxToast.backgroundColour` | str | `"black"` | Màu nền của toast trên Termux. |
| `captcha.toastOrPopup.termuxToast.textColour` | str | `"red"` | Màu chữ của toast trên Termux. |
| `captcha.toastOrPopup.termuxToast.position` | str | `"middle"` | Vị trí của toast trên Termux. |
| `captcha.termux.vibrate.enabled` | bool | `true` | Rung máy khi có captcha, trên Termux. |
| `captcha.termux.vibrate.time` | int | `5` | Thời gian rung, tính bằng giây. |
| `captcha.termux.textToSpeech.enabled` | bool | `false` | Đọc to thông báo, trên Termux. |
| `captcha.termux.textToSpeech.captchaContent` | str | `"Captcha detected! owo"` | Nội dung đọc khi có captcha. |
| `captcha.termux.textToSpeech.bannedContent` | str | `"Banned from OwO!"` | Nội dung đọc khi bị ban. |

### batteryCheck

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `batteryCheck.enabled` | bool | `false` | Bật kiểm tra pin. Cần gói `psutil`. |
| `batteryCheck.minPercentage` | int | `15` | Ngưỡng pin, tính bằng phần trăm. |
| `batteryCheck.refreshInterval` | int | `120` | Chu kỳ kiểm tra pin, tính bằng giây. |

### channelSwitcher

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `channelSwitcher.enabled` | bool | `false` | Bật chuyển kênh. Cần `allow_custom_channels` là `true` trong `config/danger.toml`. |
| `channelSwitcher.allUsers.channels` | list | `[]` | Danh sách ID kênh dùng khi chuyển kênh. Bản phát hành để trống; thêm ID kênh trước khi bật. |
| `channelSwitcher.users` | list | `[]` | Danh sách ID người dùng có cấu hình chuyển kênh riêng. |
| `channelSwitcher.interval` | list | `[20, 40]` | Khoảng chờ giữa hai lần chuyển, tính bằng giây. |
| `channelSwitcher.delayBeforeSwitch` | list | `[2, 4]` | Khoảng chờ trước khi chuyển, tính bằng giây. |

### debug

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `debug.replyTrace` | bool | `false` | Ghi vết phản hồi để gỡ lỗi. Xem `core/reply_trace.py`. |
| `debug.replyTraceMaxMB` | int | `200` | Dung lượng tối đa, tính bằng MB, của file vết. |
| `debug.blockedLoopStackAfter` | int | `5` | Sau bao nhiêu giây vòng lặp bị chặn thì ghi ngăn xếp (stack) của luồng. |
| `debug.gatewayStats` | bool | `true` | Đếm số gói tin gateway để thống kê kết nối. |

### autoStop

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `autoStop.enabled` | bool | `true` | Bật tự dừng khi không dùng `--max-runtime`. |
| `autoStop.afterMinutes` | int | `133` | Dừng sau bao nhiêu phút. |
| `autoStop.graceSeconds` | int | `90` | Thời gian tối đa, tính bằng giây, để mỗi tài khoản hoàn tất trước khi thoát. |
| `autoStop.logEveryMinutes` | int | `30` | Cứ bao nhiêu phút in một dòng nhắc thời gian còn lại. Đặt `0` để không in. |

### levelCheck

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `levelCheck.enabled` | bool | `true` | Bật kiểm tra cấp độ. Cần Tesseract. |
| `levelCheck.intervalMinutes` | int | `1440` | Khoảng cách, tính bằng phút, giữa hai lần kiểm tra cấp độ. |
| `levelCheck.firstCheckMinDelayMinutes` | int | `15` | Lần kiểm tra đầu tiên của mỗi tài khoản được chọn ngẫu nhiên trong khoảng này, tính bằng phút. Đây là giá trị tối thiểu. |
| `levelCheck.firstCheckMaxDelayMinutes` | int | `75` | Giá trị tối đa của khoảng trên, tính bằng phút. |
| `levelCheck.maxChecksPerMinuteAllAccounts` | int | `2` | Số lần kiểm tra tối đa mỗi phút cho tất cả tài khoản. |
| `levelCheck.replyTimeoutSeconds` | int | `45` | Số giây chờ phản hồi của OwO. |
| `levelCheck.command` | str | `"lvl"` | Lệnh gửi tới OwO để xem cấp độ. |
| `levelCheck.tesseractPath` | str | `"third_party/tesseract/tesseract.exe"` | Đường dẫn tới Tesseract. Để nguyên nếu dùng `setup.bat`. Khoá, chỉ sửa tay. |
| `levelCheck.saveFailedCards` | bool | `false` | Lưu ảnh thẻ không đọc được để xem lại. |
| `levelCheck.debug` | bool | `true` | Ghi nhật ký mỗi lần kiểm tra vào `utils/data/level_debug.jsonl`. |

## Khối gateway (tuỳ chọn)

Khối này không có trong file mặc định. Dashboard đọc và ghi khối này trong `config/global_settings.json`. Khoá thiếu thì bot dùng giá trị mặc định trong mã.

| Khoá | Kiểu | Mặc định | Ý nghĩa |
|---|---|---|---|
| `gateway.lean` | bool | `true` | Gateway gọn: bot chỉ nhận sự kiện từ kênh spam và DM với OwO. `false` trả về hành vi cũ của thư viện. |
| `gateway.chunkGuilds` | bool | `false` | Yêu cầu danh sách thành viên của mọi server khi khởi động. |
| `gateway.guildSubscriptions` | bool | `false` | Đăng ký nhận sự kiện của mọi server. |
| `gateway.maxMessages` | int | `200` | Số tin nhắn lưu tạm, từ 0 đến 1000. Số 0 tắt bộ nhớ đệm, khi đó các sự kiện sửa tin nhắn của blackjack, slots, coinflip không chạy. |
| `gateway.requestGuilds` | bool | `true` | Thử nghiệm: tên cũ của `guildSubscriptions`, chỉ dùng với thư viện chưa có `guildSubscriptions`. |

## config/misc.json

Không nên sửa trừ khi bạn hiểu rõ.

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `debug.logInTextFile` | bool | `true` | Ghi bản chữ của console vào `logs/`. |
| `debug.logFolder` | str | `"logs"` | Thư mục nhật ký. |
| `debug.logMaxMB` | int | `20` | Dung lượng, tính bằng MB, khi file nhật ký đạt thì mở file mới. |
| `debug.logKeepFiles` | int | `20` | Số file nhật ký giữ lại tối đa. |
| `debug.hideUser` | bool | `false` | Thay tên người dùng bằng tên ngẫu nhiên khi hiển thị. |
| `debug.consoleLevel` | str | `"INFO"` | Mức nhật ký hiện trên console: `DEBUG`, `INFO`, `WARNING`, `ERROR` hoặc `CRITICAL`. |
| `debug.fileLevel` | str | `"DEBUG"` | Mức nhật ký ghi vào file trong `logs/`, cùng các giá trị như trên. |
| `debug.captchaTrace.enabled` | bool | `false` | Ghi vết các bước xử lý captcha vào `logs/captcha/`, mỗi lượt captcha một file. Tắt mặc định. |
| `debug.captchaTrace.level` | str | `"DEBUG"` | Mức nhật ký của vết captcha. |
| `debug.captchaTrace.includeDmRaw` | bool | `false` | Ghi cả nội dung thô của tin nhắn DM vào vết captcha. Nên để tắt. |
| `debug.captchaTrace.maxFileMB` | int | `20` | Dung lượng tối đa, tính bằng MB, của một file vết captcha. Khi vượt, file được đổi tên và file mới được tạo. |
| `debug.captchaTrace.maxFiles` | int | `5` | Số file vết captcha giữ lại tối đa. |
| `news` | bool | `true` | Tải và hiện tin tức từ API của dự án gốc khi khởi động. |
| `console.compactMode` | bool | `false` | Ẩn banner và dòng phiên bản khi khởi động. |
| `console.disableCommandSendLog` | bool | `false` | Tắt dòng nhật ký mỗi khi gửi lệnh. |
| `hostMode` | bool | `false` | Chạy trên máy không có màn hình: tắt thông báo hệ thống, kiểm tra pin và cửa sổ popup. |
| `alias.hunt.normal` | str | `"hunt"` | Tên chuẩn của lệnh `hunt`. |
| `alias.hunt.shortform` | str | `"h"` | Tên viết tắt của lệnh `hunt`. |
| `alias.hunt.alias` | list | (danh sách bí danh) | Danh sách bí danh của lệnh `hunt`. |
| `command_info.hunt.priority` | int | `2` | Thứ tự ưu tiên của lệnh `hunt`. Số nhỏ được ưu tiên hơn. |
| `command_info.hunt.basecd` | int | `15` | Thời gian chờ cơ bản của lệnh `hunt`, tính bằng giây. |
| `command_info.hunt.log_color` | str | `"#8b76e6"` | Màu của dòng nhật ký lệnh `hunt`. |

Các mục `alias.*` và `command_info.*` có cùng cách đặt khoá cho từng lệnh.

## config/danger.toml

Không nên sửa trừ khi bạn hiểu rõ. Đọc chú thích trong file trước khi bật.

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `allow_auto_quest` | bool | `false` | Cho phép tự làm nhiệm vụ (quest). Tính năng thử nghiệm. |
| `auto_quest_accounts` | list | `[]` | Các tài khoản được tự làm nhiệm vụ. Mỗi mục là ID người dùng Discord đầy đủ, hoặc 4 chữ số cuối của ID; `"*"` là mọi tài khoản; danh sách rỗng là không tài khoản nào. |
| `allow_quotes` | bool | `true` | Cho phép lấy câu nói từ API bên ngoài (`blv-anh-ngok-said.onrender.com`) cho lệnh tăng cấp. |
| `allow_custom_channels` | bool | `false` | Cho phép chuyển kênh (`channelSwitcher`). Chuyển kênh có rủi ro. |

## config/captcha.toml

Bộ giải captcha. Các khoá dưới đây được mô tả ở mức khoá.

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `notifications.notify_when_attempting_to_solve` | bool | `true` | Gửi thông báo khi bot bắt đầu thử xử lý captcha. Chú thích trong file ghi: đặt `false` để tắt, nhưng không khuyến nghị. |
| `image_solver.enabled` | bool | `false` | Bộ giải captcha ảnh nội bộ, miễn phí, chạy trên máy. Mặc định tắt. Khi bật, bot có thể thử giải captcha ảnh và gửi đáp án bằng tin nhắn riêng. |
| `hcaptcha_solver.auto_detect_enterprise` | bool | `true` | Tự nhận biết captcha loại enterprise. Theo chú thích trong file, mục này chỉ áp dụng cho `captchaly`. |
| `hcaptcha_solver.captchaly.enabled` | bool | `false` | Bật nhà cung cấp `captchaly`. Dịch vụ bên thứ ba, tắt mặc định. |
| `hcaptcha_solver.captchaly.priority` | int | `1` | Thứ tự thử. Số nhỏ được thử trước. |
| `hcaptcha_solver.captchaly.api_key` | str | `""` | Khoá API của dịch vụ. Đây là bí mật, không chia sẻ. |
| `hcaptcha_solver.captchaly.min_retries` | int | `3` | Số lần thử tối thiểu. Hai nhà cung cấp dùng chung khoảng 3 đến 5. |
| `hcaptcha_solver.captchaly.max_retries` | int | `5` | Số lần thử tối đa. |
| `hcaptcha_solver.yescaptcha.enabled` | bool | `false` | Bật nhà cung cấp `yescaptcha`. Dịch vụ bên thứ ba, tắt mặc định. |
| `hcaptcha_solver.yescaptcha.priority` | int | `2` | Thứ tự thử. Số nhỏ được thử trước. |
| `hcaptcha_solver.yescaptcha.api_key` | str | `""` | Khoá API của dịch vụ. Đây là bí mật, không chia sẻ. |
| `hcaptcha_solver.yescaptcha.min_retries` | int | `3` | Số lần thử tối thiểu. |
| `hcaptcha_solver.yescaptcha.max_retries` | int | `5` | Số lần thử tối đa. |

Hai nhà cung cấp `captchaly` và `yescaptcha` là dịch vụ trả phí của bên thứ ba, và chúng tắt trong bản phát hành. Tài liệu này không hướng dẫn cách dùng chúng.

## config/webhookContent.json

Mẫu nội dung thông báo gửi qua webhook. Mỗi mẫu có các trường `title`, `description`, `content`, `color`, `thumbnail`, `author_name`, `author_image` và `footer`. Chỉ có tác dụng khi `webhook.enabled` bật. Bản phát hành có sẵn mẫu cho từng loại dưới đây.

| Khoá | Kiểu | Mặc định (bản phát hành) | Ý nghĩa |
|---|---|---|---|
| `on_captcha` | object | có sẵn | Mẫu thông báo khi phát hiện captcha. |
| `on_captcha_solve` | object | có sẵn | Mẫu thông báo kết quả xử lý captcha, dùng khi nhà cung cấp bên thứ ba bật. |
| `on_captcha_solve_fail` | object | có sẵn | Mẫu thông báo khi xử lý captcha thất bại, dùng khi nhà cung cấp bên thứ ba bật. |
| `on_captcha_solve_no_credits` | object | có sẵn | Mẫu thông báo khi nhà cung cấp bên thứ ba hết số dư. |
| `on_captcha_timeout_flag` | object | có sẵn | Mẫu thông báo khi hết thời gian chờ captcha (`captcha.timeoutSeconds`). |
| `on_captcha_ban` | object | có sẵn | Mẫu thông báo khi phát hiện bị ban. |
| `on_account_stopped` | object | có sẵn | Mẫu thông báo khi một tài khoản bị dừng. |
| `lootbox` | object | có sẵn | Mẫu thông báo khi tìm được lootbox. |
| `crate` | object | có sẵn | Mẫu thông báo khi tìm được crate. |
| `on_channel_switch` | object | có sẵn | Mẫu thông báo khi đổi kênh. |
| `daily_claim` | object | có sẵn | Mẫu thông báo khi nhận daily. |
| `on_hunt_catch` | object | có sẵn | Mẫu thông báo khi bắt được thú từ lệnh hunt. |
| `on_multiple_hunt_catch` | object | có sẵn | Mẫu thông báo khi một lần hunt bắt được nhiều thú. |
| `on_command_send` | object | có sẵn | Mẫu thông báo mỗi lần gửi lệnh. Cần `webhook.logCommandSend` là `true`. |

## Tệp khác trong config và audio

- `config/level_lines.txt`: danh sách câu, mỗi dòng một câu. Dùng cho lệnh tăng cấp khi `commands.lvlGrind.useQuoteInstead` bật. Dòng trống bị bỏ qua.
- `audio/Beep.mp3`: âm thanh mặc định khi có captcha, là giá trị mặc định của `captcha.playAudio.path`. Thêm file khác vào `audio/` rồi đổi tên trong `captcha.playAudio.path`, hoặc ghi đường dẫn đầy đủ tới file.
