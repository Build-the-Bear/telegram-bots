# Henry the Hypemachine - Order of Events:
    # 1. Potentially sendRandomMessage
    # 2. Parse response from getTelegramUpdates
    # 3. Respond to mentions
    # 4. Respond to commands
    # 5. Respond to interesting threads, then messages
    # 6. Potentially sendSticker
    # 7. Rinse and repeat

# import packages
import requests
import logging
import random
import openai
import boto3
import math
import time
import time
import ast
import os

from henryPrompts import *
from boto3.dynamodb.conditions import Key
from dotenv import load_dotenv

# load environment variables
load_dotenv("./.env")

# set up API keys
telegramAPIKey = os.getenv("PROD_TELEGRAM_API_KEY")
openai.api_key = os.getenv("OPENAI_API_KEY")

# connect to dynamodb on aws
dynamodb = boto3.resource("dynamodb", aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID"),
                                  aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"), region_name = "us-east-2")
chatInfo = dynamodb.Table('chat_info')

if telegramAPIKey == os.getenv('DEV_TELEGRAM_API_KEY'):
    chatInfo = dynamodb.Table("henry_test_chat_information")

# designate log location
logging.basicConfig(filename="henry.log", level=logging.INFO)

# define environment variables
lastUpdateID = -1  # offset response from getTelegramUpdates
lastChatIDs = [0, 0, 0]  # chat IDs for the last few messages, to prevent flooding

existingChats = {}  # e.g. {"-1001640903207": "Last message sent"}
existingReplies = {}  # e.g. {"-1001640903207": [100, 250, 3000 . . .
existingSettings = {}  # e.g. {"-1001640903207": {"toggleMentions":"on", "toggleReplies":"on" . . .

# ensure we're not flooding chats or api endpoints
temporarilyIgnoredChatID = 0
timeIgnored = time.time()

lastPriceCheck = time.time()
lastPriceMessage = ""

# fetch recent updates from telegram
def getTelegramUpdates(startup):
    global lastUpdateID

    # offset by last updates retrieved
    url = "https://api.telegram.org/" + telegramAPIKey + "/getupdates?offset=" + str(lastUpdateID + 1)
    updates = requests.get(url)
    response = updates.json()["result"]

    if len(response):
        lastUpdateID = response[len(response) - 1]["update_id"]

    # logging.info(response)

    # check new messages
    if not startup:
        for i in response:
            mes = i["message"]

            if "message" in i and "text" in mes:
                fro = mes["from"]["username"]
                mid = mes["message_id"]
                cid = mes["chat"]["id"]
                txt = mes["text"]

                checkForNewChatID(cid)

                # respond to mentions with context
                if ("reply_to_message" in mes and isSentence(txt) and
                    "username" in mes["reply_to_message"]["from"] and
                    mes["reply_to_message"]["from"]["username"].startswith("Henrythe")) and haveNotReplied(cid, mid):
                        triggerPrompt = fro + ": " + txt + "\n"

                        if "text" in mes["reply_to_message"]:
                            triggerPrompt = "Henry the Hypemachine: " + mes["reply_to_message"]["text"] + "\n" + fro + ": " + txt + "\n"

                        respondToMention(triggerPrompt, cid, mid)

                # if an any-case match was found for one of henry's commands, and he hasn't already, respond
                for c in henryCommands:
                    commandFound = False

                    if "text" in mes and anyCaseMatch(c, txt):
                        commandFound = True

                    # check if command requires administrator rights
                    if commandFound and haveNotReplied(cid, mid) and "toggle" in c:
                        if fromAdmin(str(cid), str(mes["from"]["id"])):
                            cc = txt.replace(c, "")

                            toggleSetting(cid, mid, c, cc.strip().lower())
                        else:
                            sendResponse(cid, mid, "I don't take orders from you")

                    if commandFound and haveNotReplied(cid, mid) and "prices" in c:
                        sendResponse(cid, mid, checkPrices(lastPriceMessage))

                    if commandFound and haveNotReplied(cid, mid) and "toggle" not in c:
                        sendResponse(cid, mid, henryCommands[c])

                # if an any-case match was found for one of henry's triggers, and he hasn't already, respond
                for t in triggerMessages:
                    triggerFound = False

                    if "text" in mes and anyCaseMatch(t, txt):
                        triggerFound = True

                    if triggerFound and isSentence(txt) and haveNotReplied(cid, mid):
                        triggerPrompt = ""

                        # if the matching message happens to be a reply itself, get thread context
                        if "reply_to_message" in mes:
                            triggerPrompt = mes["reply_to_message"]["from"]["username"] + ": " + mes["reply_to_message"]["text"] + "\n" + fro + ": " + txt + "\n"
                        else:
                            triggerPrompt = fro + ": " + txt

                        triggerResponse(triggerPrompt, cid, mid)

# fetch existing chats_ids from aws
def getExistingChatInformation():
    response = chatInfo.scan()

    for i in response["Items"]:
        if "last_reply" in i and i["last_reply"] is not None:
            existingChats[i["chat_id"]] = i["last_reply"]
        else:
            existingChats[i["chat_id"]] = ""

        if "chat_settings" in i and i["chat_settings"] is not None:
            existingSettings[i["chat_id"]] = i["chat_settings"]
        else:
            existingSettings[i["chat_id"]] = {}

        existingReplies[str(i["chat_id"])] = ast.literal_eval(i["chat_replies"])

# save new chat information
def checkForNewChatID(chatID):
    if chatID not in existingChats:
        existingChats[chatID] = ""
        existingReplies[str(chatID)] = [0, 1]

        chatInfo.put_item(Item={"chat_id": chatID, "chat_replies": str([0, 1]), "chat_settings": {}})

# determine if chat is a group chat
def isGroupChat(chatID):
    type = "undetermined"

    try:
        url = "https://api.telegram.org/" + telegramAPIKey + "/getChat?chat_id=" + str(chatID)
        updates = requests.get(url)

        if "result" in updates.json() and "type" in updates.json()["result"] : type = updates.json()["result"]["type"]

        if type != "private": return True
        else: return False
    except requests.exceptions.HTTPError as err:
        logging.info("Henry was met with a closed door: " + err)

# determine if string is a sentence
def isSentence(s):
    return len(s.split()) > 1

# determine whether we're flooding a single chat or not
def checkFlood(chatID, timeIgnored):
    if time.time() - timeIgnored > 1800:
        temporarilyIgnoredChatID = 0
        timeIgnored = time.time()

    if lastChatIDs[0] == chatID and lastChatIDs[1] == chatID and lastChatIDs[2] == chatID:
        temporarilyIgnoredChatID = chatID

    else: temporarilyIgnoredChatID = 0

# determine whether a given message has been replied to or not
def haveNotReplied(chatID, messageID):
    if messageID not in existingReplies[str(chatID)]:
        return True
    else:
        return False

# determine whether a message came from an admin or not
def fromAdmin(chatID, userID):
    try:
        url = "https://api.telegram.org/" + telegramAPIKey + "/getChatMember?chat_id=" + chatID + "&user_id=" + userID
        updates = requests.get(url)
        response = updates.json()["result"]

        if len(response):
            if "status" in response and (response["status"] == "administrator" or response["status"] == "creator"):
                return True
            else: return False
    except requests.exceptions.HTTPError as err:
        logging.info("Henry couldn't figure out how to open the door: " + err)

# determine if parse string has any-case match
def anyCaseMatch(match, parse):
    if match in parse or match.lower() in parse or match.upper() in parse: return True
    else: return False

# artificial seasoning
def spice(message, isReply, optionalPrompt):
    # construct message
    mess = "mess"
    r = random.randint(1, 10)

    # only reply 60% of the time Henry gets triggered
    if isReply == False and r > 6 and not anyCaseMatch("Henry", message):
        mess = ""

    if mess != "":
        # if no specific prompt was provided, choose a random one
        if optionalPrompt == "": optionalPrompt = defaultPrompt

        try:
            # season the prompt
            response = openai.Completion.create(
              model = "text-davinci-002",
              prompt = optionalPrompt + "\n\n'" + message + "'",
              temperature = 1.1,
              max_tokens = 65,
              top_p = 1,
              frequency_penalty = 0.9,
              presence_penalty = 0.9,
            )

            mess = response.choices[0].text.strip()
        except requests.exceptions.HTTPError as err:
            logging.info("Henry couldn't figure out how to open the door: " + err)

        # clean up the presentation
        mapping = [ ("Henry the Hypemachine:", ""),
                    ("HenrytheHypemachine:", ""),
                    ("'HenrytheHypemachine':", ""),
                    ("Henry:", ""),
                    ("lordhenry:", ""),
                    ("HenrytheHypeBot:", ""),
                    ("Henry the Hypemachine responds:", ""),
                    ("?\"", ""),
                    ("ors", "ooors")]

        for k, v in mapping:
            mess = mess.replace(k, v)

        if len(mess) > 0 and (mess[0] == '"' or mess[0] == "'"): mess = mess[1:]
        if len(mess) > 0 and (mess[-1] == '"' or mess[-1] == "'"): mess = mess[:-1]

    return mess.strip()

# respond to direct mentions
def respondToMention(toMessage, chatID, messageID):
    cid = str(chatID)
    mess = ""

    if checkSetting(chatID, "/toggleMentions") != "off":
        mess = spice(toMessage, True, "")

    if existingChats[chatID] != mess and mess != "":
        sendResponse(chatID, messageID, mess)

# trigger unique responses by keyword
def triggerResponse(toMessage, chatID, messageID):
    sendIt = True
    mess = ""

    checkFlood(chatID, timeIgnored)

    # prevent flooding an individual chat in production
    if telegramAPIKey == os.getenv('PROD_TELEGRAM_API_KEY') and not anyCaseMatch("Henry", toMessage):
        sendIt = False
    else:
        if checkSetting(chatID, "/toggleReplies") != "off":
            mess = spice(toMessage, False, "")

    # if the message was constructed and should be sent
    if existingChats[chatID] != mess and mess != "" and temporarilyIgnoredChatID != chatID and sendIt:
        sendResponse(chatID, messageID, mess)

# trigger random responses
def sendRandomMessage(shouldSend):
    chatID = random.choice(list(existingChats))
    mess = ""

    checkFlood(chatID, timeIgnored)

    # prevent flooding an individual chat in production
    if telegramAPIKey == os.getenv('PROD_TELEGRAM_API_KEY'):
        shouldSend = False
    else:
        if checkSetting(chatID, "/toggleRandomMessages") != "off":
            mess = spice(random.choice(randomMessages), False, "")

        while isGroupChat(chatID) != True and mess != "":
            chatID = random.choice(list(existingChats))

    # if the message was constructed and should be sent
    if existingChats[chatID] != mess and mess != "" and shouldSend and temporarilyIgnoredChatID != chatID:
        try:
            url = "https://api.telegram.org/" + telegramAPIKey + "/sendMessage?chat_id=" + str(chatID) + "&text=" + mess
            x = requests.post(url, json={})

            updateDatabase(chatID, existingReplies[str(chatID)], existingSettings[chatID], mess)

            lastChatIDs.pop(0)
            lastChatIDs.append(chatID)

            logging.info("Henry had some words to say in Chat " + str(chatID) + ": " + mess)
        except requests.exceptions.HTTPError as err:
            logging.info("Henry was met with a closed door: " + err)

# send henry's message(s) off to the telegram api
def sendResponse(chatID, messageID, message):
    cid = str(chatID)
    mid = str(messageID)

    try:
        existingChats[chatID] = message

        url = "https://api.telegram.org/" + telegramAPIKey + "/sendMessage?chat_id=" + cid + "&reply_to_message_id=" + mid + "&text=" + message
        x = requests.post(url, json={})

        # update local and database lists with new messageID
        if existingReplies[cid] is None:
            existingReplies[cid] = [0, 1]

        existingReplies[cid].append(messageID)

        replies = existingReplies[cid]
        settings = existingSettings[chatID]

        updateDatabase(chatID, replies, settings, message)

        lastChatIDs.pop(0)
        lastChatIDs.append(chatID)

        time.sleep(2)

        logging.info("Henry had some words to say in Chat " + cid + ": " + message)

        if checkSetting(chatID, "/toggleStickers") != "off":
            # send a random BtB sticker 20% of the time
            r = random.randint(1, 10)

            if r < 3:
                stickerID = random.choice(list(stickerIDs))

                url = "https://api.telegram.org/" + telegramAPIKey + "/sendSticker?chat_id=" + str(chatID) + "&sticker=" + stickerID
                x = requests.post(url, json={})

                logging.info("Henry sent a sticker to Chat " + cid + ": " + stickerID)
    except requests.exceptions.HTTPError as err:
        logging.info("Henry was met with a closed door: " + err)

# update database
def updateDatabase(chatID, replies, settings, lastReply):
    if settings is None:
        settings = {}

    try:
        response = chatInfo.update_item(
            Key={
                "chat_id": chatID,
            },
            UpdateExpression="set #chat_replies=:r, #last_reply=:l, #chat_settings=:s",
            ExpressionAttributeNames={
                "#chat_replies": "chat_replies",
                "#last_reply": "last_reply",
                "#chat_settings": "chat_settings",
            },
            ExpressionAttributeValues={
                ":r": str(replies),
                ":l": lastReply,
                ":s": settings,
            },
            ReturnValues="UPDATED_NEW"
        )
    except requests.exceptions.HTTPError as err:
        logging.info("Henry was met with a closed door: " + err)

# functions specific to Build the Bear channels
def nowBuildTheBear():
    logging.info("Henry is Building the Bear")

# toggle settings for a given chat
def toggleSetting(chatID, messageID, setting, value):
    if (setting != "/toggleMentions" and setting != "/toggleReplies" and setting != "/toggleRandomMessages" and setting != "/toggleStickers") or (value != "on" and value != "off"):
        sendResponse(chatID, messageID, "Wrong format, please try again")
    else:
        mess = spice("Please change this setting, Henry. Thank you.", True, "")
        existingSettings[chatID][setting] = value
        sendResponse(chatID, messageID, mess)

# check settings for a given chat
def checkSetting(chatID, setting):
    if setting in existingSettings[chatID]:
        return existingSettings[chatID][setting]
    else: return "on"

# check market prices
def checkPrices(lastPriceMessage):
    currentPriceMessage = spice("Please check the market prices, Henry.", True, "")
    currentPrice = 0

    if (time.time() - lastPriceCheck > 59) or lastPriceMessage == "":
        currentPriceMessage += "\n\n▔▔▔▔▔▔▔▔▔▔"

        try:
            url = 'https://api.binance.us/api/v3/ticker?symbols=["BTCUSDT","ETHUSDT","BNBUSDT"]'
            updates = requests.get(url)
            response = updates.json()

            # logging.info(response)

            for i in response:
                currentPrice = "{:.2f}".format(float(i["lastPrice"]))
                currentPriceMessage += "\n" + i["symbol"][:3] + "   ➤   $" + str(currentPrice)
        except requests.exceptions.HTTPError as err:
            logging.info("Henry was met with a closed door: " + err)

        lastPriceMessage = currentPriceMessage

    return currentPriceMessage

# initialize
if __name__ == "__main__":
    # get existing chat information and new updates off the rip
    getExistingChatInformation()
    getTelegramUpdates(True)

    chatCount = len(list(existingChats))
    runningTime, lastMessageTime, oneDaysTime = 0, 0, 86400
    waitTime, checkTime = 10, 15

    if chatCount < (oneDaysTime / 10):
        waitTime = oneDaysTime / chatCount

    # while running
    while True:
        if runningTime % round(waitTime, -1) == 0:
            sendRandomMessage(True)

        runningTime += checkTime
        time.sleep(checkTime)

        getExistingChatInformation()
        getTelegramUpdates(False)
        # nowBuildTheBear()
