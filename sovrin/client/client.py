import json
import traceback
from _sha256 import sha256
from base64 import b64decode
from collections import deque
from copy import deepcopy
from typing import Mapping, List, Dict, Union, Tuple, Optional

import base58
import pyorient

from raet.raeting import AutoMode

from sovrin.client import roles
from plenum.client.client import Client as PlenumClient
from plenum.server.router import Router
from plenum.client.signer import Signer
from plenum.common.startable import Status
from plenum.common.stacked import SimpleStack
from plenum.common.txn import REPLY, STEWARD, ENC, HASH, RAW, NAME, VERSION,\
    KEYS, TYPE, IP, PORT
from plenum.common.types import OP_FIELD_NAME, Request, f, HA, OPERATION
from plenum.common.util import getlogger, getSymmetricallyEncryptedVal, \
    libnacl, error
from plenum.persistence.orientdb_store import OrientDbStore
from sovrin.client.wallet import Wallet
from sovrin.common.txn import TXN_TYPE, ATTRIB, DATA, TXN_ID, TARGET_NYM, SKEY,\
    DISCLO, NONCE, GET_ATTR, GET_NYM, ROLE, \
    SPONSOR, NYM, GET_TXNS, LAST_TXN, TXNS, GET_TXN, CRED_DEF, GET_CRED_DEF
from sovrin.common.util import getConfig
from sovrin.persistence.client_req_rep_store_file import ClientReqRepStoreFile
from sovrin.persistence.client_req_rep_store_orientdb import \
    ClientReqRepStoreOrientDB
from sovrin.persistence.client_txn_log import ClientTxnLog
from sovrin.persistence.identity_graph import getEdgeByTxnType, IdentityGraph

from sovrin.anon_creds.issuer import Issuer
from sovrin.anon_creds.prover import Prover
from sovrin.anon_creds.verifier import Verifier

logger = getlogger()


# DEPR
# class Client(PlenumClient, Issuer, Prover, Verifier):
class Client(PlenumClient):
    def __init__(self,
                 name: str,
                 nodeReg: Dict[str, HA]=None,
                 ha: Union[HA, Tuple[str, int]]=None,
                 peerHA: Union[HA, Tuple[str, int]]=None,
                 basedirpath: str=None,
                 config=None,
                 postReplyConsClbk=None):
        config = config or getConfig()
        super().__init__(name,
                         nodeReg,
                         ha,
                         basedirpath,
                         config)
        # self.storage = self.getStorage(basedirpath)
        # self.lastReqId = self.storage.lastReqId
        # TODO: Should I store values of attributes as non encrypted
        # Dictionary of attribute requests
        # Key is request id and values are stored as tuple of 5 elements
        # identifier, toNym, secretKey, attribute name, txnId
        # self.attributeReqs = self.storage.loadAttributes()
        # type: Dict[int, List[Tuple[str, str, str, str, str]]]
        self.graphStore = self.getGraphStore()
        self.autoDiscloseAttributes = False
        self.requestedPendingTxns = False
        # DEPR
        # Issuer.__init__(self, self.defaultIdentifier)
        # Prover.__init__(self, self.defaultIdentifier)
        # Verifier.__init__(self, self.defaultIdentifier)
        dataDirs = ["data/{}s".format(r) for r in roles]

        # To make anonymous credentials optional, we may have a subclass
        #  of Sovrin Client instead that mixes in Issuer, Prover and
        #  Verifier.
        self.hasAnonCreds = bool(peerHA)
        if self.hasAnonCreds:
            self.peerHA = peerHA if isinstance(peerHA, HA) else HA(*peerHA)
            stackargs = dict(name=name,
                             ha=peerHA,
                             main=True,
                             auto=AutoMode.always)
            self.peerMsgRoutes = []
            self.peerMsgRouter = Router(*self.peerMsgRoutes)
            self.peerStack = SimpleStack(stackargs,
                                         msgHandler=self.handlePeerMessage)
            self.peerStack.sign = self.sign
            self.peerInbox = deque()
        self.postReplyConsClbk = postReplyConsClbk

    #DEPR
    # def setupWallet_DEPRECATED(self, wallet=None):
    #     if wallet:
    #         self.wallet = wallet
    #     else:
    #         storage = WalletStorageFile.fromName(self.name, self.basedirpath)
    #         self.wallet = Wallet(self.name, storage)

    def handlePeerMessage(self, msg):
        """
        Use the peerMsgRouter to pass the messages to the correct
         function that handles them

        :param msg: the P2P client message.
        """
        return self.peerMsgRouter.handle(msg)

    # DEPR
    # def sign_DEPRECATED(self, msg: Dict, signer: Signer) -> Dict:
    #     if msg[OPERATION].get(TXN_TYPE) == ATTRIB:
    #         msgCopy = deepcopy(msg)
    #         keyName = {RAW, ENC, HASH}.intersection(
    #             set(msgCopy[OPERATION].keys())).pop()
    #         msgCopy[OPERATION][keyName] = sha256(msgCopy[OPERATION][keyName]
    #                                                .encode()).hexdigest()
    #         msg[f.SIG.nm] = signer.sign(msgCopy)
    #         return msg
    #     else:
    #         return super().sign(msg, signer)

    def _getOrientDbStore(self):
        return OrientDbStore(user=self.config.OrientDB["user"],
                             password=self.config.OrientDB["password"],
                             dbName=self.name,
                             storageType=pyorient.STORAGE_TYPE_PLOCAL)

    def getReqRepStore(self):
        if self.config.ReqReplyStore == "orientdb":
            return ClientReqRepStoreOrientDB(self._getOrientDbStore())
        else:
            return ClientReqRepStoreFile(self.name, self.basedirpath)

    def getGraphStore(self):
        return IdentityGraph(self._getOrientDbStore()) if \
            self.config.ClientIdentityGraph else None

    def getTxnLogStore(self):
        return ClientTxnLog(self.name, self.basedirpath)

    #DEPR
    def submit_DEPRECATED(self, *operations: Mapping, identifier: str=None) -> \
            List[Request]:
        origin = identifier or self.defaultIdentifier
        for op in operations:
            if op[TXN_TYPE] == ATTRIB:
                if not (RAW in op or ENC in op or HASH in op):
                    error("An operation must have one of these keys: {} "
                          "or {} {}".format(RAW, ENC, HASH))

                # TODO: Consider encryption type too.
                if ENC in op:
                    anm = list(json.loads(op[ENC]).keys())[0]
                    encVal, secretKey = getSymmetricallyEncryptedVal(op[ENC])
                    op[ENC] = encVal
                    self.wallet.addAttribute(name=anm, val=encVal,
                                             origin=origin,
                                             dest=op.get(TARGET_NYM),
                                             encKey=secretKey)
                # TODO: Consider hash type too.
                elif HASH in op:
                    data = json.loads(op[HASH])
                    anm = list(data.keys())[0]
                    aval = list(data.values())[0]
                    hashed = sha256(aval.encode()).hexdigest()
                    op[HASH] = {anm: hashed}
                    self.wallet.addAttribute(name=anm, val=aval,
                                             origin=origin,
                                             dest=op.get(TARGET_NYM),
                                             hashed=True)
                else:
                    data = json.loads(op[RAW])
                    anm = list(data.keys())[0]
                    aval = list(data.values())[0]
                    self.wallet.addAttribute(name=anm, val=aval,
                                             origin=origin,
                                             dest=op.get(TARGET_NYM))
            if op[TXN_TYPE] == CRED_DEF:
                data = op.get(DATA)
                keys = data[KEYS]
                self.wallet.addCredDef(data[NAME], data[VERSION],
                                       origin, data[TYPE],
                                       data[IP], data[PORT], keys)
        requests = super().submit(*operations, identifier=identifier)
        return requests

    def handleOneNodeMsg(self, wrappedMsg, excludeFromCli=None) -> None:
        msg, sender = wrappedMsg
        excludeFromCli = excludeFromCli or (msg.get(OP_FIELD_NAME) == REPLY
                                            and msg[f.RESULT.nm][TXN_TYPE] == GET_TXNS)
        super().handleOneNodeMsg(wrappedMsg, excludeFromCli)
        if OP_FIELD_NAME not in msg:
            logger.error("Op absent in message {}".format(msg))

    def postReplyRecvd(self, reqId, frm, result, numReplies):
        reply = super().postReplyRecvd(reqId, frm, result, numReplies)
        # TODO: Use callback here
        # if reply and self.postReplyConsClbk:
        if reply:
            if isinstance(self.reqRepStore, ClientReqRepStoreOrientDB):
                self.reqRepStore.setConsensus(reqId)
            if result[TXN_TYPE] == NYM:
                if self.graphStore:
                    self.addNymToGraph(result)
            elif result[TXN_TYPE] == ATTRIB:
                if self.graphStore:
                    self.graphStore.addAttribTxnToGraph(result)
            elif result[TXN_TYPE] == GET_NYM:
                if self.graphStore:
                    if DATA in result and result[DATA]:
                        self.addNymToGraph(json.loads(result[DATA]))
            elif result[TXN_TYPE] == GET_TXNS:
                if DATA in result and result[DATA]:
                    data = json.loads(result[DATA])
                    self.reqRepStore.setLastTxnForIdentifier(
                        result[f.IDENTIFIER.nm], data[LAST_TXN])
                    if self.graphStore:
                        for txn in data[TXNS]:
                            if txn[TXN_TYPE] == NYM:
                                self.addNymToGraph(txn)
                            elif txn[TXN_TYPE] == ATTRIB:
                                try:
                                    self.graphStore.addAttribTxnToGraph(txn)
                                except pyorient.PyOrientCommandException as ex:
                                    logger.error(
                                        "An exception was raised while adding "
                                        "attribute {}".format(ex))
                                    logger.trace(traceback.format_exc())

            elif result[TXN_TYPE] == CRED_DEF:
                if self.graphStore:
                    self.graphStore.addCredDefTxnToGraph(result)
            elif result[TXN_TYPE] == GET_CRED_DEF:
                data = result.get(DATA)
                try:
                    data = json.loads(data)
                    keys = json.loads(data[KEYS])
                except Exception as ex:
                    # Checking if data was converted to JSON, if it was then
                    #  exception was raised while converting KEYS
                    # TODO: Check fails if data was a dictionary.
                    if isinstance(data, dict):
                        logger.error(
                            "Keys {} cannot be converted to JSON"
                                .format(data[KEYS]))
                    else:
                        logger.error("{} cannot be converted to JSON"
                                     .format(data))
                else:
                    self.wallet.addCredDef(data[NAME], data[VERSION],
                                           result[TARGET_NYM], data[TYPE],
                                           data[IP], data[PORT], keys)

    def requestConfirmed(self, reqId: int) -> bool:
        if isinstance(self.reqRepStore, ClientReqRepStoreOrientDB):
            return self.reqRepStore.requestConfirmed(reqId)
        else:
            return self.txnLog.hasTxnWithReqId(reqId)

    def hasConsensus(self, reqId: int) -> Optional[str]:
        if isinstance(self.reqRepStore, ClientReqRepStoreOrientDB):
            return self.reqRepStore.hasConsensus(reqId)
        else:
            return super().hasConsensus(reqId)

    def addNymToGraph(self, txn):
        origin = txn.get(f.IDENTIFIER.nm)
        if txn.get(ROLE) == SPONSOR:
            if not self.graphStore.hasSteward(origin):
                try:
                    self.graphStore.addNym(None, nym=origin, role=STEWARD)
                except pyorient.PyOrientCommandException as ex:
                    logger.trace("Error occurred adding nym to graph")
                    logger.trace(traceback.format_exc())
        self.graphStore.addNymTxnToGraph(txn)

    def getTxnById(self, txnId: str):
        if self.graphStore:
            txns = list(self.graphStore.getResultForTxnIds(txnId).values())
            return txns[0] if txns else {}
        else:
            # TODO: Add support for fetching reply by transaction id
            # serTxn = self.reqRepStore.getResultForTxnId(txnId)
            pass
        # TODO Add merkleInfo as well

    def getTxnsByNym(self, nym: str):
        # TODO Implement this
        pass

    def getTxnsByType(self, txnType):
        if self.graphStore:
            edgeClass = getEdgeByTxnType(txnType)
            if edgeClass:
                cmd = "select from {}".format(edgeClass)
                result = self.graphStore.client.command(cmd)
                if result:
                    return [r.oRecordData for r in result]
            return []
        else:
            txns = self.txnLog.getTxnsByType(txnType)
            # TODO: Fix ASAP
            if txnType == CRED_DEF:
                for txn in txns:
                    txn[DATA] = json.loads(txn[DATA].replace("\'", '"')
                                           .replace('"{', '{')
                                           .replace('}"', '}'))
                    txn[NAME] = txn[DATA][NAME]
                    txn[VERSION] = txn[DATA][VERSION]
            return txns

    # TODO: Just for now. Remove it later
    # DEPR
    def doAttrDisclose_DEPRECATED(self, origin, target, txnId, key):
        box = libnacl.public.Box(b64decode(origin), b64decode(target))

        data = json.dumps({TXN_ID: txnId, SKEY: key})
        nonce, boxedMsg = box.encrypt(data.encode(), pack_nonce=False)

        op = {
            TARGET_NYM: target,
            TXN_TYPE: DISCLO,
            NONCE: base58.b58encode(nonce),
            DATA: base58.b58encode(boxedMsg)
        }
        self.submit(op, identifier=origin)

    # DEPR
    def doGetAttributeTxn_DEPRECATED(self, identifier, attrName):
        # Getting public attribute only
        op = {
            TARGET_NYM: identifier,
            TXN_TYPE: GET_ATTR,
            RAW: attrName
        }
        return self.submit(op, identifier=identifier)

    @staticmethod
    def _getDecryptedData(encData, key):
        data = bytes(bytearray.fromhex(encData))
        rawKey = bytes(bytearray.fromhex(key))
        box = libnacl.secret.SecretBox(rawKey)
        decData = box.decrypt(data).decode()
        return json.loads(decData)

    # DEPR
    def getAttributeForNym_DEPRECATED(self, nym, attrName, identifier=None):
        walletAttribute = self.wallet.getAttribute(attrName, nym)
        if walletAttribute:
            if TARGET_NYM in walletAttribute and \
                            walletAttribute[TARGET_NYM] == nym:
                if RAW in walletAttribute:
                    if walletAttribute[NAME] == attrName:
                        return {walletAttribute[NAME]: walletAttribute[RAW]}
                elif ENC in walletAttribute:
                    attr = self._getDecryptedData(walletAttribute[ENC],
                                           walletAttribute[SKEY])
                    if attrName in attr:
                        return attr
                elif HASH in walletAttribute:
                    if walletAttribute[NAME] == attrName:
                        return {walletAttribute[NAME]: walletAttribute[HASH]}

    # DEPR
    def getAllAttributesForNym_DEPRECATED(self, nym, identifier=None):
        # TODO: Does this need to get attributes from the nodes?
        walletAttributes = self.wallet.attributes
        attributes = []
        for attr in walletAttributes:
            if TARGET_NYM in attr and attr[TARGET_NYM] == nym:
                if RAW in attr:
                    attributes.append({attr[NAME]: attr[RAW]})
                elif ENC in attr:
                    attributes.append(self._getDecryptedData(attr[ENC],
                                                             attr[SKEY]))
                elif HASH in attr:
                    attributes.append({attr[NAME]: attr[HASH]})
        return attributes

    # DEPR
    def doGetNym_DEPRECATED(self, nym, identifier=None):
        identifier = identifier if identifier else self.defaultIdentifier
        op = {
            TARGET_NYM: nym,
            TXN_TYPE: GET_NYM,
        }
        self.submit(op, identifier=identifier)

    # DEPR
    def doGetTxn_DEPRECATED(self, txnId, identifier=None):
        identifier = identifier if identifier else self.defaultIdentifier
        op = {
            TARGET_NYM: identifier,
            TXN_TYPE: GET_TXN,
            DATA: txnId
        }
        self.submit(op, identifier=identifier)

    def hasNym(self, nym):
        if self.graphStore:
            return self.graphStore.hasNym(nym)
        else:
            for txn in self.txnLog.getTxnsByType(NYM):
                if txn.get(TXN_TYPE) == NYM:
                    return True
            return False

    # DEPR
    # def requestPendingTxns(self):
    #     requests = []
    #     for identifier in self.signers:
    #         lastTxn = self.reqRepStore.getLastTxnForIdentifier(identifier)
    #         op = {
    #             TARGET_NYM: identifier,
    #             TXN_TYPE: GET_TXNS,
    #         }
    #         if lastTxn:
    #             op[DATA] = lastTxn
    #         requests.append(self.submit(op, identifier=identifier))
    #     return requests

    def _statusChanged(self, old, new):
        super()._statusChanged(old, new)
        # DEPR
        # if new == Status.started:
        #     if not self.requestedPendingTxns:
        #         self.requestPendingTxns()
        #         self.requestedPendingTxns = True

    def start(self, loop):
        super().start(loop)
        if self.hasAnonCreds and \
                        self.status is not Status.going():
            self.peerStack.start()

    async def prod(self, limit) -> int:
        s = await self.nodestack.service(limit)
        if self.isGoing():
            await self.nodestack.serviceLifecycle()
        self.nodestack.flushOutBoxes()
        if self.hasAnonCreds:
            return s + await self.peerStack.service(limit)
        else:
            return s
