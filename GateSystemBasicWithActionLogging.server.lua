script.Parent.Values.GateNumber.Value = script.Parent.Name
script.CamAnimScript.Value.Value = script.Parent.ScannerModel

--[[ FINAL CALL ]]

local TweenService = game:GetService("TweenService")
local HttpService = game:GetService("HttpService")

-- Set the "WebhookUrl" attribute on this Script to your Discord-compatible webhook URL.
-- HTTP Requests must also be enabled in Game Settings > Security.
local WEBHOOK_URL = script:GetAttribute("WebhookUrl") or ""
local warnedAboutMissingWebhook = false

local function auditLog(player, action)
	local gateNumber = script.Parent.Values.GateNumber.Value
	local actor = string.format(
		"%s (@%s, UserId %d)",
		player.DisplayName,
		player.Name,
		player.UserId
	)
	local outputMessage = string.format(
		"[Gate Audit] %s | By: %s | Gate: %s",
		action,
		actor,
		tostring(gateNumber)
	)

	print(outputMessage)

	if WEBHOOK_URL == "" then
		if not warnedAboutMissingWebhook then
			warn("[Gate Audit] WebhookUrl is not configured on the Script; webhook logging is disabled.")
			warnedAboutMissingWebhook = true
		end
		return
	end

	task.spawn(function()
		local requestSucceeded, response = pcall(function()
			return HttpService:RequestAsync({
				Url = WEBHOOK_URL,
				Method = "POST",
				Headers = {
					["Content-Type"] = "application/json",
				},
				Body = HttpService:JSONEncode({
					username = "Gate Audit",
					embeds = {
						{
							title = action,
							color = 3447003,
							fields = {
								{
									name = "Staff member",
									value = actor,
									inline = false,
								},
								{
									name = "Gate",
									value = tostring(gateNumber),
									inline = true,
								},
							},
							timestamp = os.date("!%Y-%m-%dT%H:%M:%SZ"),
						},
					},
				}),
			})
		end)

		if not requestSucceeded then
			warn("[Gate Audit] Webhook request failed:", response)
		elseif not response.Success then
			warn(
				"[Gate Audit] Webhook returned",
				response.StatusCode,
				response.StatusMessage,
				response.Body
			)
		end
	end)
end

local finalCallFadeTween = nil
local finalCallFadeRunning = false

local function StartFinalCallFade(boardScreen)
	local FINAL_RED = Color3.fromRGB(221, 72, 72)
	local FINAL_BLUE = Color3.fromRGB(0, 0, 0)

	if finalCallFadeRunning then
		return
	end

	finalCallFadeRunning = true

	task.spawn(function()
		local useBlue = true

		while finalCallFadeRunning and boardScreen and boardScreen.Parent do
			local targetColor = useBlue and FINAL_BLUE or FINAL_RED
			useBlue = not useBlue

			if finalCallFadeTween then
				finalCallFadeTween:Cancel()
			end

			finalCallFadeTween = TweenService:Create(
				boardScreen,
				TweenInfo.new(1.4, Enum.EasingStyle.Sine, Enum.EasingDirection.InOut),
				{ BackgroundColor3 = targetColor }
			)

			finalCallFadeTween:Play()
			finalCallFadeTween.Completed:Wait()
		end
	end)
end

local function StopFinalCallFade()
	finalCallFadeRunning = false

	if finalCallFadeTween then
		finalCallFadeTween:Cancel()
		finalCallFadeTween = nil
	end
end

--[[ SCREEN ]]

script.Parent.Monitor.LCD.ClickDetector.MouseClick:Connect(function(player)
	if player:GetRankInGroupAsync(35238318) <= 5 then
		return
	end

	local playerGui = player:FindFirstChildOfClass("PlayerGui")
	if not playerGui then return end

	local ui = playerGui:FindFirstChild("31273123621")

	if ui then
		ui.Holder.LocalScript.FORCECLOSE.Value = true
		task.wait(.6)
		ui:Destroy()
		auditLog(player, "Close screen")
	else
		script.Parent.Monitor.LCD.SurfaceGui.GateClosedScreen.Visible = false
		script.Parent.Monitor.LCD.SurfaceGui.FakeControlsScreen.Visible = true

		local newUi = script.UI:Clone()
		newUi.Parent = playerGui
		newUi.Name = "31273123621"

		newUi.Holder.LocalScript.Values.Value = script.Parent.Values
		auditLog(player, "Open screen")

		local tempclientremote = Instance.new("RemoteEvent")
		tempclientremote.Parent = game.ReplicatedStorage
		tempclientremote.Name = tostring(math.random(192121, 19212121))

		newUi.Holder.LocalScript.Remote.Value = tempclientremote

		tempclientremote.OnServerEvent:Connect(function(plr, x, y)
			if plr ~= player then
				return
			end

			if x == "Screen" then
				script.Parent.Values.Screen.Value = y
				script.Parent.GateScreen.Board.SurfaceGui.GateClosedScreen.Visible = not y
				auditLog(player, y and "Screen on" or "Screen off")

			elseif x == "GateStatus" then
				script.Parent.Values.GateStatus.Value = y
				game.Workspace.SERVERINFORMATIONCLIENT.Gate_Status.Value = y

				local boardscreen = script.Parent.GateScreen.Board.SurfaceGui["BOARDING STATUS"]
				boardscreen.STATUS.Text = y

				if y == "Gate Closed" then
					StopFinalCallFade()
					boardscreen.Parent.ADVISORY.THING.Text = ""
					boardscreen.BackgroundColor3 = Color3.fromRGB(221, 72, 72)

				elseif y == "Final Call" then
					boardscreen.Parent.ADVISORY.THING.Text = "Prepare your ID / Passport and Boarding Card"
					StartFinalCallFade(boardscreen)

				else
					StopFinalCallFade()
					boardscreen.BackgroundColor3 = Color3.fromRGB(144, 221, 63)
					boardscreen.Parent.ADVISORY.THING.Text = "Prepare your ID / Passport and Boarding Card"
				end

			elseif x == "Priority" then
				script.Parent.Values.Priority.Value = y
				auditLog(player, (y and "Start" or "Stop") .. " boarding for class Priority")

			elseif x == "Standard" then
				script.Parent.Values.Standard.Value = y
				auditLog(player, (y and "Start" or "Stop") .. " boarding for class Standard")
			end
		end)
	end
end)

local function UPD()
	local sic = game.Workspace:WaitForChild("SERVERINFORMATIONCLIENT")

	script.Parent.GateScreen.Board.SurfaceGui.TOPFRAME.FLIGHTNO.Text =
		sic.FlightCode.Value

	script.Parent.GateScreen.Board.SurfaceGui.TOPFRAME.ARRIVAL.Text =
		string.upper(sic.Arrival.Value)

	script.Parent.Monitor.LCD.SurfaceGui.FakeControlsScreen.DESTINATION.Text =
		string.upper(sic.Arrival.Value)

	script.Parent.Monitor.LCD.SurfaceGui.FakeControlsScreen.FLIGHTNUMBER.Text =
		string.upper(sic.FlightCode.Value)

	script.Parent.Monitor.LCD.SurfaceGui.FakeControlsScreen.GATESTATUS.Text =
		string.upper(sic.Gate_Status.Value)
end

UPD()

game.Workspace:WaitForChild("SERVERINFORMATIONCLIENT").Arrival:GetPropertyChangedSignal("Value"):Connect(UPD)
game.Workspace:WaitForChild("SERVERINFORMATIONCLIENT").Gate_Status:GetPropertyChangedSignal("Value"):Connect(UPD)
game.Workspace:WaitForChild("SERVERINFORMATIONCLIENT").FlightCode:GetPropertyChangedSignal("Value"):Connect(UPD)

--[[ BOARDING SYS ]]

local function GateScannerSystem(Priority, Standard)
	local Players = game:GetService("Players")
	local RunService = game:GetService("RunService")
	local ReplicatedStorage = game:GetService("ReplicatedStorage")

	local GROUP_ID = 35238318
	local MIN_RANK = 6

	local Values = Priority.Parent
	local ROOT = Values.Parent

	local Boarded = Values:WaitForChild("Boarded")
	local Remaining = Values:WaitForChild("Remaining")

	local GATE = ROOT
	local TICKET_SCANNER = GATE:WaitForChild("ticketScanner")
	local SENSOR = TICKET_SCANNER:WaitForChild("Sensor")
	local DEST = TICKET_SCANNER:WaitForChild("TeleportPos")

	local CAM_SCRIPT_TEMPLATE = script:WaitForChild("CamAnimScript")

	local SCANNER_MODEL =
		script:FindFirstChild("ScannerModel") and script.ScannerModel.Value
		or GATE:FindFirstChild("ScannerModel")
		or TICKET_SCANNER:FindFirstChild("ScannerModel")
		or TICKET_SCANNER

	local UpgradeNotification = require(
		ReplicatedStorage:WaitForChild("BL_NotificationSystem")
	)

	local ENTER_RADIUS = 12
	local EXIT_RADIUS = 15
	local CHECK_DT = 0.15
	local SCAN_LOCK_TIME = 6.2
	local TELEPORT_OFFSET = CFrame.new(0, 3, 0)

	local ENTER_R2 = ENTER_RADIUS * ENTER_RADIUS
	local EXIT_R2 = EXIT_RADIUS * EXIT_RADIUS

	local playerInZone = {}
	local playerScanLockedUntil = {}
	local playerScannedFirstTime = {}
	local passportNotifyCooldown = {}

	local TOOL_ENABLED = {
		["Fast Track"] = false,
		["Family Fast Track"] = false,
		["Standard"] = false,
	}

	local function getOrCreateInt(parent, name)
		local value = parent:FindFirstChild(name)

		if not value then
			value = Instance.new("IntValue")
			value.Name = name
			value.Parent = parent
		end

		return value
	end

	local function updateServerBoardingInfo()
		local sic = workspace:FindFirstChild("SERVERINFORMATIONCLIENT")
		if not sic then
			return
		end

		getOrCreateInt(sic, "Boarded").Value = tonumber(Boarded.Value) or 0
		getOrCreateInt(sic, "Remaining").Value = tonumber(Remaining.Value) or 0
	end

	local function isStaff(player)
		local ok, rank = pcall(function()
			return player:GetRankInGroup(GROUP_ID)
		end)

		return ok and rank >= MIN_RANK
	end

	local function updateTicketToggles()
		TOOL_ENABLED["Fast Track"] = Priority.Value
		TOOL_ENABLED["Family Fast Track"] = Priority.Value
		TOOL_ENABLED["Standard"] = Standard.Value
	end

	local function updateCounts()
		local sic = workspace:FindFirstChild("SERVERINFORMATIONCLIENT")
		if not sic then
			Remaining.Value = 0
			updateServerBoardingInfo()
			return
		end

		local checkedFolder = sic:FindFirstChild("CheckedInPlayers")
		if not checkedFolder then
			Remaining.Value = 0
			updateServerBoardingInfo()
			return
		end

		local checkedInNonStaff = 0

		for _, folder in ipairs(checkedFolder:GetChildren()) do
			local player = Players:FindFirstChild(folder.Name)

			if player then
				if not isStaff(player) then
					checkedInNonStaff += 1
				end
			else
				checkedInNonStaff += 1
			end
		end

		local boardedCount = tonumber(Boarded.Value) or 0
		Remaining.Value = math.max(checkedInNonStaff - boardedCount, 0)

		updateServerBoardingInfo()
	end

	local function dist2(a, b)
		local d = a - b
		return d.X * d.X + d.Y * d.Y + d.Z * d.Z
	end

	local function getEquippedTool(character)
		for _, child in ipairs(character:GetChildren()) do
			if child:IsA("Tool") then
				return child
			end
		end

		return nil
	end

	local function playerHasFastTrack(player)
		local sic = workspace:FindFirstChild("SERVERINFORMATIONCLIENT")
		if not sic then return false end

		local checkedInPlayers = sic:FindFirstChild("CheckedInPlayers")
		if not checkedInPlayers then return false end

		local playerFolder = checkedInPlayers:FindFirstChild(player.Name)
		if not playerFolder then return false end

		local upgrades = playerFolder:FindFirstChild("Upgrades")
		if not upgrades then return false end

		local priorityBoarding = upgrades:FindFirstChild("PriorityBoarding")
		return priorityBoarding
			and priorityBoarding:IsA("BoolValue")
			and priorityBoarding.Value == true
	end

	local function notifyScanPhone(player)
		local now = os.clock()

		if passportNotifyCooldown[player] and now - passportNotifyCooldown[player] < 4 then
			return
		end

		passportNotifyCooldown[player] = now

		UpgradeNotification.Notify(
			player,
			"Boarding Pass Required",
			"Please scan your phone to board.",
			4
		)
	end

	local function getValidScanTool(player)
		local character = player.Character
		if not character then return nil end

		local tool = getEquippedTool(character)
		if not tool then return nil end

		local boardingEnabled =
			TOOL_ENABLED["Standard"] == true
			or TOOL_ENABLED["Fast Track"] == true
			or TOOL_ENABLED["Family Fast Track"] == true

		-- Passport = notify ONLY if some boarding lane is enabled
		if tool.Name == "PASSPORT" or tool.Name == "Passport" then
			if boardingEnabled then
				notifyScanPhone(player)
			end

			return nil
		end

		-- iPhone = scan only if boarding is enabled
		if tool.Name == "iPhone" then
			if playerScannedFirstTime[player] then
				return nil
			end

			if TOOL_ENABLED["Standard"] == true then
				return tool, "Phone"
			end

			if TOOL_ENABLED["Fast Track"] == true or TOOL_ENABLED["Family Fast Track"] == true then
				if playerHasFastTrack(player) then
					return tool, "Phone"
				end
			end

			return nil
		end

		if TOOL_ENABLED[tool.Name] == true then
			return tool, "Ticket"
		end

		return nil
	end

	local function isScanLocked(player)
		local untilTime = playerScanLockedUntil[player]
		return untilTime and time() < untilTime
	end

	local function lockScan(player)
		playerScanLockedUntil[player] = time() + SCAN_LOCK_TIME
	end

	local function injectScannerCam(player, propName)
		local playerGui = player:FindFirstChildOfClass("PlayerGui")
		if not playerGui then return end

		if not SCANNER_MODEL or not SCANNER_MODEL:IsA("Model") then
			warn("[GateScanner] Invalid SCANNER_MODEL")
			return
		end

		local old = playerGui:FindFirstChild("CamAnimScript")
		if old then
			old:Destroy()
		end

		local ls = CAM_SCRIPT_TEMPLATE:Clone()
		ls.Name = "CamAnimScript"
		ls.Enabled = false

		local scannerOV = ls:FindFirstChild("ScannerModel")
		if not scannerOV then
			scannerOV = Instance.new("ObjectValue")
			scannerOV.Name = "ScannerModel"
			scannerOV.Parent = ls
		end

		scannerOV.Value = SCANNER_MODEL

		local propSV = ls:FindFirstChild("Prop")
		if not propSV then
			propSV = Instance.new("StringValue")
			propSV.Name = "Prop"
			propSV.Parent = ls
		end

		propSV.Value = propName

		ls.Parent = playerGui

		task.defer(function()
			if ls and ls.Parent then
				ls.Enabled = true
			end
		end)
	end

	local function teleportPlayer(player)
		local character = player.Character
		if not character then return end

		local hrp = character:FindFirstChild("HumanoidRootPart")
		if not hrp then return end

		character:PivotTo(DEST.CFrame * TELEPORT_OFFSET)
	end

	local function onPlayerScannedFirstTime(player)
		local module = game.ServerScriptService:FindFirstChild("Check-In Module")
		if not module then return end

		local seatHighlight = module:FindFirstChild("SeatHighlightSystem")
		if not seatHighlight then return end

		local playerGui = player:FindFirstChildOfClass("PlayerGui")
		if not playerGui then return end

		local old = playerGui:FindFirstChild("SeatHighlightSystem")
		if old then
			old:Destroy()
		end

		local cloned = seatHighlight:Clone()
		cloned.Parent = playerGui
		cloned.Enabled = true
	end

	local function onPlayerScanned(player, toolName, propName)
		print("[GateScanner] Scanned:", player.Name, toolName, propName)

		if not playerScannedFirstTime[player] then
			playerScannedFirstTime[player] = true

			Boarded.Value = (tonumber(Boarded.Value) or 0) + 1
			updateCounts()
			updateServerBoardingInfo()

			onPlayerScannedFirstTime(player)
		end
	end

	local function scanPlayer(player, tool, propName)
		if isScanLocked(player) then
			return
		end

		lockScan(player)
		injectScannerCam(player, propName)
		teleportPlayer(player)
		onPlayerScanned(player, tool.Name, propName)
	end

	Priority.Changed:Connect(function()
		updateTicketToggles()
	end)

	Standard.Changed:Connect(function()
		updateTicketToggles()
	end)

	local function hookCheckedInFolder()
		local sic = workspace:WaitForChild("SERVERINFORMATIONCLIENT")
		local checkedFolder = sic:WaitForChild("CheckedInPlayers")

		checkedFolder.ChildAdded:Connect(function()
			updateCounts()
			updateServerBoardingInfo()
		end)

		checkedFolder.ChildRemoved:Connect(function()
			updateCounts()
			updateServerBoardingInfo()
		end)

		updateCounts()
		updateServerBoardingInfo()
	end

	task.spawn(hookCheckedInFolder)

	Players.PlayerAdded:Connect(function()
		task.defer(function()
			updateCounts()
			updateServerBoardingInfo()
		end)
	end)

	Players.PlayerRemoving:Connect(function(player)
		playerInZone[player] = nil
		playerScanLockedUntil[player] = nil
		playerScannedFirstTime[player] = nil
		passportNotifyCooldown[player] = nil

		task.defer(function()
			updateCounts()
			updateServerBoardingInfo()
		end)
	end)

	Boarded.Changed:Connect(function()
		updateCounts()
		updateServerBoardingInfo()
	end)

	Remaining.Changed:Connect(function()
		updateServerBoardingInfo()
	end)

	updateTicketToggles()
	updateCounts()
	updateServerBoardingInfo()

	local acc = 0

	RunService.Heartbeat:Connect(function(dt)
		acc += dt

		if acc < CHECK_DT then
			return
		end

		acc = 0
		updateTicketToggles()

		for _, player in ipairs(Players:GetPlayers()) do
			local character = player.Character
			local hrp = character and character:FindFirstChild("HumanoidRootPart")

			if not hrp then
				playerInZone[player] = nil
				continue
			end

			local distanceSquared = dist2(hrp.Position, SENSOR.Position)

			if distanceSquared > EXIT_R2 then
				playerInZone[player] = false
				continue
			end

			if distanceSquared <= ENTER_R2 then
				local tool, propName = getValidScanTool(player)

				if tool then
					if not playerInZone[player] then
						playerInZone[player] = true
						scanPlayer(player, tool, propName)
					elseif not isScanLocked(player) then
						scanPlayer(player, tool, propName)
					end
				end
			end
		end
	end)
end

GateScannerSystem(script.Parent.Values.Priority, script.Parent.Values.Standard)
